"""Gemini SDKの通信を模擬し、実SDKを使うテストへ共通の接続準備を提供する。"""

import json
from dataclasses import dataclass, field

import httpx
import httpx2
import pytest
from pydantic import SecretStr

from app.ai_providers.gemini import client as module
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from tests.test_http._proxy_exchange import ProxyExchange, install_proxy_exchange


@dataclass
class SdkExchange(ProxyExchange):
    headers: list[tuple[bytes, bytes]] = field(
        default_factory=lambda: [(b"content-type", b"application/json")]
    )
    clients: list[httpx2.AsyncClient] = field(default_factory=list)

    def respond_json(self, payload):
        self.body.chunks = (json.dumps(payload).encode(),)

    def respond_sse(self, *payloads):
        self.headers = [(b"content-type", b"text/event-stream")]
        self.body.chunks = tuple(
            f"data: {json.dumps(payload)}\n\n".encode() for payload in payloads
        )

    def request_json(self, index):
        return json.loads(self.request_bodies[index])


def install_sdk_exchange(monkeypatch: pytest.MonkeyPatch) -> SdkExchange:
    """DNSとproxy送受信を模擬し、注入したHTTPX2 client以外からの送信を拒否する。"""
    result = SdkExchange()
    install_proxy_exchange(monkeypatch, result)
    original_factory = module.make_external_async_client
    original_send = httpx2.AsyncClient.send

    def factory(**kwargs):
        client = original_factory(**kwargs)
        result.clients.append(client)
        return client

    async def send_only_injected(self, *args, **kwargs):
        assert self in result.clients, "SDK bypassed the injected HTTP client"
        return await original_send(self, *args, **kwargs)

    def reject_sync_send(*args, **kwargs):
        pytest.fail("SDK attempted synchronous HTTP I/O")

    async def reject_legacy_send(*args, **kwargs):
        pytest.fail("SDK sent through the legacy httpx client")

    monkeypatch.setattr(module, "make_external_async_client", factory)
    monkeypatch.setattr(httpx2.AsyncClient, "send", send_only_injected)
    monkeypatch.setattr(httpx2.Client, "send", reject_sync_send)
    # SDKは旧httpxにも依存するため、そちらから送信していないことも確かめる。
    monkeypatch.setattr(httpx.AsyncClient, "send", reject_legacy_send)
    monkeypatch.setattr(httpx.Client, "send", reject_sync_send)
    return result


def open_gemini_test_client():
    return module.open_gemini_client(
        api_key=SecretStr("synthetic-test-key"),
        settings=GeminiConnectionSettings(
            connect_timeout=2, read_timeout=7, write_timeout=11, pool_timeout=3
        ),
    )
