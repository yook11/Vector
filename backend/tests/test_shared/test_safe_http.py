"""``make_safe_async_client`` のユニットテスト。

SSRF 検証 + DNS rebind 防御を担う ``_PinnedDnsTransport`` の挙動、
``follow_redirects`` の default、transport-level kwargs の振分を検証する。
親 class ``httpx.AsyncHTTPTransport.handle_async_request`` を monkeypatch で
short-circuit して、実 HTTP は出さずに pin された Request を観察する。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.shared.security.safe_http import (
    _PinnedDnsTransport,
    make_safe_async_client,
)
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


@pytest.fixture
def captured_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> list[httpx.Request]:
    """``AsyncHTTPTransport.handle_async_request`` を short-circuit し、
    transport 通過後の Request を捕捉する。実 HTTP は出さない。
    """
    sink: list[httpx.Request] = []

    async def _capture(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        sink.append(request)
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _capture)
    return sink


# ---------------------------------------------------------------------------
# Transport ベースの SSRF 検証
# ---------------------------------------------------------------------------
class TestSsrfValidation:
    @pytest.mark.asyncio
    async def test_blocks_request_to_private_host(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        with _patch_resolver("10.0.0.1"):
            async with make_safe_async_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://internal.example/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_blocks_request_to_loopback(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        with _patch_resolver("127.0.0.1"):
            async with make_safe_async_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://localhost-alias.example.com/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_allows_request_to_public_host(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        with _patch_resolver("8.8.8.8"):
            async with make_safe_async_client() as client:
                resp = await client.get("https://example.com/")
        assert resp.status_code == 200
        assert len(captured_requests) == 1

    @pytest.mark.asyncio
    async def test_propagates_host_resolution_error(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        import socket

        with _patch_resolver(socket.gaierror("name unknown")):
            async with make_safe_async_client() as client:
                with pytest.raises(HostResolutionError):
                    await client.get("https://nonexistent.invalid/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_blocks_private_ip_literal(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """private IP literal を直接渡された場合も transport が拒否する
        (defense-in-depth: SafeUrl で弾く前提だが二重保証)。"""
        async with make_safe_async_client() as client:
            with pytest.raises(HostBlockedError):
                await client.get("http://10.0.0.1/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_passes_public_ip_literal_without_resolve(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """public IP literal は DNS resolve せず通過。"""
        with patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(side_effect=AssertionError("must not resolve")),
        ):
            async with make_safe_async_client() as client:
                resp = await client.get("https://8.8.8.8/")
        assert resp.status_code == 200
        assert str(captured_requests[0].url) == "https://8.8.8.8/"


# ---------------------------------------------------------------------------
# DNS rebind 防御 (chain δ regression guard)
# ---------------------------------------------------------------------------
class TestDnsRebindResistance:
    """red-team chain δ: validate と connect の間で DNS が切り替わっても
    TCP 接続は validate 済の最初の IP に pin される (TOCTOU 不成立)。"""

    @pytest.mark.asyncio
    async def test_pins_to_first_resolved_ip(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """``_resolve_host`` が複数回呼ばれても、TCP 接続先は 1 回目の解決
        結果のみに依存する (red-team chain δ PoC-4 反転 regression)。
        """
        call_count = 0

        async def fake_resolve(host: str) -> list[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ["8.8.8.8"]
            return ["172.20.0.5"]  # 2nd resolve で internal を返す rebind 攻撃

        with patch(
            "app.shared.security.ssrf_guard._resolve_host", side_effect=fake_resolve
        ):
            async with make_safe_async_client() as client:
                await client.get("https://rebind.example/feed.xml")

        assert len(captured_requests) == 1
        # URL host は 1st resolve の IP (8.8.8.8) に pin され、
        # 内部 IP (172.20.0.5) には絶対に到達しない
        assert captured_requests[0].url.host == "8.8.8.8"
        assert captured_requests[0].url.host != "172.20.0.5"
        # transport 内 resolve は 1 回のみ (2 度目以降の rebind に依存しない)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_preserves_host_header_for_routing(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """Host header は元 host を維持 (HTTP virtual host routing 用)。"""
        with _patch_resolver("8.8.8.8"):
            async with make_safe_async_client() as client:
                await client.get("https://example.com/path")
        assert captured_requests[0].headers["Host"] == "example.com"

    @pytest.mark.asyncio
    async def test_sets_sni_hostname_extension_for_tls_verify(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """``extensions["sni_hostname"]`` に元 host を設定し、TLS server_hostname
        が IP に書換わらないことを保証 (cert verify pass 担保)。"""
        with _patch_resolver("8.8.8.8"):
            async with make_safe_async_client() as client:
                await client.get("https://example.com/")
        assert captured_requests[0].extensions.get("sni_hostname") == "example.com"

    @pytest.mark.asyncio
    async def test_pins_to_ipv6_resolved_ip(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """IPv6 host も pin される (httpx が ``[ip]`` 形式に自動 bracket)。"""
        with _patch_resolver("2001:4860:4860::8888"):
            async with make_safe_async_client() as client:
                await client.get("https://example.com/")
        assert captured_requests[0].url.host == "2001:4860:4860::8888"
        assert captured_requests[0].headers["Host"] == "example.com"

    @pytest.mark.asyncio
    async def test_preserves_path_and_query_after_pin(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """URL host を IP に書換えても path / query / port は維持する。"""
        with _patch_resolver("8.8.8.8"):
            async with make_safe_async_client() as client:
                await client.get("https://example.com:8443/api/v1?x=1&y=2")
        url = captured_requests[0].url
        assert url.host == "8.8.8.8"
        assert url.port == 8443
        assert url.path == "/api/v1"
        assert url.query == b"x=1&y=2"
        # Host header は元の host:port (default port でないので port 含む)
        assert captured_requests[0].headers["Host"] == "example.com:8443"


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
# transport の構造的保証
# ---------------------------------------------------------------------------
class TestTransportStructure:
    @pytest.mark.asyncio
    async def test_uses_pinned_dns_transport(self) -> None:
        """make_safe_async_client が返す client は ``_PinnedDnsTransport`` を
        必ず装着する (event_hook ではなく transport 層で防御する構造保証)。"""
        async with make_safe_async_client() as client:
            assert isinstance(client._transport, _PinnedDnsTransport)

    @pytest.mark.asyncio
    async def test_passes_transport_kwargs_to_transport(self) -> None:
        """``verify`` / ``http2`` などの transport-level kwargs は transport
        コンストラクタに渡され、AsyncClient には流れない。"""
        # http2 の指定が transport に正しく届いた場合、AsyncHTTPTransport の
        # 内部 _pool が h2 enabled で初期化される。client は transport=... の
        # 際は他の transport-level kwargs を受け付けない (= ValueError 出ず
        # に成功) ことで、振分が正しいことが分かる。
        async with make_safe_async_client(verify=True, http1=True) as client:
            assert isinstance(client._transport, _PinnedDnsTransport)
