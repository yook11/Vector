"""DNSの返答とproxyより下の送受信だけを差し替え、共通HTTP経路を実際に通す。

httpcore2のproxy送信口を差し替えるため、httpcore2の内部構成が変わったらここだけを直す。
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import httpcore2
import pytest

PROXY_URL = "http://proxy.vector.internal:3128"
PUBLIC_ADDRESS = "93.184.216.34"


@dataclass
class ResponseBody:
    """読まれたチャンクと解放を観測でき、指定すると最後のチャンクの後で止まる応答本文。"""

    chunks: tuple[bytes, ...] = ()
    consumed: list[bytes] = field(default_factory=list)
    closed: bool = False
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.consumed.append(chunk)
            yield chunk
        if self.resume is not None:
            self.waiting.set()
            await self.resume.wait()

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class ProxyExchange:
    """proxyへ渡った要求を記録し、指定した応答または失敗を返す。"""

    status: int = 200
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: ResponseBody = field(default_factory=ResponseBody)
    requests: list[httpcore2.Request] = field(default_factory=list)
    request_bodies: list[bytes] = field(default_factory=list)
    error: Exception | None = None


def install_proxy_exchange(
    monkeypatch: pytest.MonkeyPatch, exchange: ProxyExchange
) -> None:
    """名前解決を公開IPに固定し、proxy経由の送受信を``exchange``へ向ける。"""
    monkeypatch.setenv("EGRESS_PROXY_URL", PROXY_URL)
    monkeypatch.setattr(
        "app.http.destination_resolution._resolve_host",
        AsyncMock(return_value=[PUBLIC_ADDRESS]),
    )

    async def respond(
        self: httpcore2.AsyncHTTPProxy, request: httpcore2.Request
    ) -> httpcore2.Response:
        exchange.requests.append(request)
        exchange.request_bodies.append(
            b"".join([chunk async for chunk in request.stream])
        )
        if exchange.error is not None:
            raise exchange.error
        return httpcore2.Response(
            exchange.status, headers=exchange.headers, content=exchange.body
        )

    monkeypatch.setattr(httpcore2.AsyncHTTPProxy, "handle_async_request", respond)
