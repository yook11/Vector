"""``make_safe_async_client`` のユニットテスト。

SSRF 検証 event_hook の動作、``follow_redirects`` の default、ユーザー指定
``event_hooks`` との merge を検証する。``MockTransport`` を使い実 HTTP は出さない。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError


def _patch_resolver(*addrs: str | Exception):
    if len(addrs) == 1 and isinstance(addrs[0], Exception):
        return patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(side_effect=addrs[0]),
        )
    return patch(
        "app.shared.security.ssrf_guard._resolve_host",
        new=AsyncMock(return_value=list(addrs)),
    )


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"ok")


# ---------------------------------------------------------------------------
# SSRF event hook の動作
# ---------------------------------------------------------------------------
class TestEventHookSsrfValidation:
    @pytest.mark.asyncio
    async def test_blocks_request_to_private_host(self) -> None:
        with _patch_resolver("10.0.0.1"):
            async with make_safe_async_client(
                transport=httpx.MockTransport(_ok_handler)
            ) as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://internal.example/")

    @pytest.mark.asyncio
    async def test_blocks_request_to_loopback(self) -> None:
        with _patch_resolver("127.0.0.1"):
            async with make_safe_async_client(
                transport=httpx.MockTransport(_ok_handler)
            ) as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://localhost-alias.example.com/")

    @pytest.mark.asyncio
    async def test_allows_request_to_public_host(self) -> None:
        with _patch_resolver("8.8.8.8"):
            async with make_safe_async_client(
                transport=httpx.MockTransport(_ok_handler)
            ) as client:
                resp = await client.get("https://example.com/")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_propagates_host_resolution_error(self) -> None:
        import socket

        with _patch_resolver(socket.gaierror("name unknown")):
            async with make_safe_async_client(
                transport=httpx.MockTransport(_ok_handler)
            ) as client:
                with pytest.raises(HostResolutionError):
                    await client.get("https://nonexistent.invalid/")


# ---------------------------------------------------------------------------
# follow_redirects の default 動作
# ---------------------------------------------------------------------------
class TestFollowRedirectsDefault:
    @pytest.mark.asyncio
    async def test_default_is_false(self) -> None:
        async with make_safe_async_client() as client:
            assert client.follow_redirects is False

    @pytest.mark.asyncio
    async def test_explicit_true_is_respected(self) -> None:
        async with make_safe_async_client(follow_redirects=True) as client:
            assert client.follow_redirects is True


# ---------------------------------------------------------------------------
# event_hooks の merge 動作
# ---------------------------------------------------------------------------
class TestEventHooksMerge:
    @pytest.mark.asyncio
    async def test_user_request_hook_runs_after_validation(self) -> None:
        """public host なら user request hook も呼ばれる。"""
        calls: list[str] = []

        async def user_hook(request: httpx.Request) -> None:
            calls.append("user")

        with _patch_resolver("8.8.8.8"):
            async with make_safe_async_client(
                transport=httpx.MockTransport(_ok_handler),
                event_hooks={"request": [user_hook]},
            ) as client:
                await client.get("https://example.com/")
        assert calls == ["user"]

    @pytest.mark.asyncio
    async def test_user_request_hook_skipped_when_validation_blocks(self) -> None:
        """validation で raise → user hook は呼ばれない (順序保証)。"""
        calls: list[str] = []

        async def user_hook(request: httpx.Request) -> None:
            calls.append("user")

        with _patch_resolver("10.0.0.1"):
            async with make_safe_async_client(
                transport=httpx.MockTransport(_ok_handler),
                event_hooks={"request": [user_hook]},
            ) as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://internal/")
        assert calls == []

    @pytest.mark.asyncio
    async def test_user_response_hook_is_preserved(self) -> None:
        """request 以外の hook (response) が壊れないこと。"""
        response_calls: list[int] = []

        async def response_hook(response: httpx.Response) -> None:
            response_calls.append(response.status_code)

        with _patch_resolver("8.8.8.8"):
            async with make_safe_async_client(
                transport=httpx.MockTransport(_ok_handler),
                event_hooks={"response": [response_hook]},
            ) as client:
                await client.get("https://example.com/")
        assert response_calls == [200]
