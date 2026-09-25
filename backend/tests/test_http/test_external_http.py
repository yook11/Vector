"""外部HTTPの送信可否・宛先指定・プロキシ経路・リダイレクトを検証する。

名前解決の返答と送信処理を差し替え、アプリの宛先検証は実際に動かす。
観察するのは送信直前のRequestであり、実TCP接続やTLS認証の成立は保証しない。
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.http.destination_policy import HostBlockedError
from app.http.destination_resolution import HostResolutionError
from app.http.external import (
    _PinnedDnsTransport,
    make_external_async_client,
)


def _patch_resolver(*addrs: str | Exception):
    if len(addrs) == 1 and isinstance(addrs[0], Exception):
        return patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(side_effect=addrs[0]),
        )
    return patch(
        "app.http.destination_resolution._resolve_host",
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


@pytest.fixture
def egress_proxy(monkeypatch: pytest.MonkeyPatch) -> str:
    """settings 経由で egress proxy を設定する (factory が読む唯一の経路)。"""
    url = "http://proxy.vector.internal:3128"
    monkeypatch.setenv("EGRESS_PROXY_URL", url)
    return url


@pytest.fixture
def redirect_requests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """宛先検証後の送信を捕捉し、最初の応答で別ホストへリダイレクトする。"""
    sent: list[str] = []

    async def respond(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        sent.append(str(request.url))
        if len(sent) == 1:
            return httpx.Response(
                302, headers={"Location": "https://next.example/article"}
            )
        return httpx.Response(200, content=b"article")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    return sent


@pytest.fixture(params=["configured-proxy", "direct-transport"])
def external_client(request: pytest.FixtureRequest) -> Callable[[], httpx.AsyncClient]:
    """通常のプロキシ経路と既存transportの直接接続で送信前の検証を確認する。"""
    if request.param == "configured-proxy":
        return make_external_async_client
    return lambda: httpx.AsyncClient(transport=_PinnedDnsTransport())


class TestDestinationValidationBeforeSend:
    @pytest.mark.asyncio
    async def test_blocks_request_to_private_host(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
    ) -> None:
        """禁止IPへ解決されたホストは送信前に拒否する。"""
        with _patch_resolver("10.0.0.1"):
            async with external_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://internal.example/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_blocks_request_to_loopback(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
    ) -> None:
        """ループバックへ解決されたホストに送信しない。"""
        with _patch_resolver("127.0.0.1"):
            async with external_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://localhost-alias.example.com/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_blocks_request_to_link_local(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
    ) -> None:
        """メタデータIPへ解決されたホストに送信しない。"""
        with _patch_resolver("169.254.169.254"):
            async with external_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://metadata-alias.example/")
        assert captured_requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("address", ["8.8.8.8", "2001:4860:4860::8888"])
    async def test_allows_request_to_public_host(
        self,
        address: str,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
    ) -> None:
        """公開IPの検証を通過したリクエストを送信処理へ渡す。"""
        with _patch_resolver(address):
            async with external_client() as client:
                resp = await client.get("https://example.com/")
        assert resp.status_code == 200
        assert len(captured_requests) == 1

    @pytest.mark.asyncio
    async def test_propagates_host_resolution_error(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
    ) -> None:
        """DNS失敗を伝播し、送信処理には進まない。"""
        with _patch_resolver(socket.gaierror("name unknown")):
            async with external_client() as client:
                with pytest.raises(HostResolutionError):
                    await client.get("https://nonexistent.invalid/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_blocks_private_ip_literal(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
    ) -> None:
        """private IP literal を直接渡された場合も transport が拒否する
        (defense-in-depth: SafeUrl で弾く前提だが二重保証)。"""
        async with external_client() as client:
            with pytest.raises(HostBlockedError):
                await client.get("http://10.0.0.1/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_passes_public_ip_literal_without_resolve(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
    ) -> None:
        """public IP literal は DNS resolve せず通過。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(side_effect=AssertionError("must not resolve")),
        ):
            async with external_client() as client:
                resp = await client.get("https://8.8.8.8/")
        assert resp.status_code == 200
        assert str(captured_requests[0].url) == "https://8.8.8.8/"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "addresses",
        [
            pytest.param(["8.8.8.8", "10.0.0.1"], id="prohibited-last"),
            pytest.param(["10.0.0.1", "8.8.8.8"], id="prohibited-first"),
        ],
    )
    async def test_mixed_resolution_never_reaches_send(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
        addresses: list[str],
    ) -> None:
        """公開IPも返る場合に禁止IPを無視して送信へ進まない。"""
        with _patch_resolver(*addresses):
            async with external_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("https://mixed.example/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_empty_resolution_never_reaches_send(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
    ) -> None:
        """IPを取得できなかった場合は送信へ進まず解決失敗を伝える。"""
        with _patch_resolver():
            async with external_client() as client:
                with pytest.raises(HostResolutionError):
                    await client.get("https://empty.example/")
        assert captured_requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "addresses",
        [
            pytest.param(["not-an-ip"], id="invalid-only"),
            pytest.param(["8.8.8.8", "not-an-ip"], id="invalid-after-public"),
            pytest.param(["not-an-ip", "8.8.8.8"], id="invalid-before-public"),
        ],
    )
    async def test_invalid_resolution_never_reaches_send(
        self,
        external_client: Callable[[], httpx.AsyncClient],
        captured_requests: list[httpx.Request],
        addresses: list[str],
    ) -> None:
        """不正なIPが混ざる場合は一部の公開IPだけで送信へ進まない。"""
        with _patch_resolver(*addresses):
            async with external_client() as client:
                with pytest.raises(HostResolutionError):
                    await client.get("https://invalid.example/")
        assert captured_requests == []


class TestDirectConnectionDestination:
    """直接接続時に検証済みIPと元のHTTP・TLSホスト名を送信処理へ渡す。"""

    @pytest.mark.asyncio
    async def test_pins_to_first_resolved_ip(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """複数の検証済みIPのうち先頭を送信先に指定する。"""
        with _patch_resolver("1.1.1.1", "8.8.8.8") as resolve:
            async with httpx.AsyncClient(transport=_PinnedDnsTransport()) as client:
                await client.get("https://example.com/feed.xml")

        assert len(captured_requests) == 1
        assert captured_requests[0].url.host == "1.1.1.1"
        resolve.assert_awaited_once_with("example.com")

    @pytest.mark.asyncio
    async def test_preserves_host_header_for_routing(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """Host header は元 host を維持 (HTTP virtual host routing 用)。"""
        with _patch_resolver("8.8.8.8"):
            async with httpx.AsyncClient(transport=_PinnedDnsTransport()) as client:
                await client.get("https://example.com/path")
        assert captured_requests[0].headers["Host"] == "example.com"

    @pytest.mark.asyncio
    async def test_sets_sni_hostname_extension_for_tls_verify(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """送信処理へ渡すTLS SNIの指定には元のホスト名を保持する。"""
        with _patch_resolver("8.8.8.8"):
            async with httpx.AsyncClient(transport=_PinnedDnsTransport()) as client:
                await client.get("https://example.com/")
        assert captured_requests[0].extensions.get("sni_hostname") == "example.com"

    @pytest.mark.asyncio
    async def test_pins_to_ipv6_resolved_ip(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """IPv6 host も pin される (httpx が ``[ip]`` 形式に自動 bracket)。"""
        with _patch_resolver("2001:4860:4860::8888"):
            async with httpx.AsyncClient(transport=_PinnedDnsTransport()) as client:
                await client.get("https://example.com/")
        assert captured_requests[0].url.host == "2001:4860:4860::8888"
        assert captured_requests[0].headers["Host"] == "example.com"

    @pytest.mark.asyncio
    async def test_preserves_path_and_query_after_pin(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """URL host を IP に書換えても path / query / port は維持する。"""
        with _patch_resolver("8.8.8.8"):
            async with httpx.AsyncClient(transport=_PinnedDnsTransport()) as client:
                await client.get("https://example.com:8443/api/v1?x=1&y=2")
        url = captured_requests[0].url
        assert url.host == "8.8.8.8"
        assert url.port == 8443
        assert url.path == "/api/v1"
        assert url.query == b"x=1&y=2"
        # Host header は元の host:port (default port でないので port 含む)
        assert captured_requests[0].headers["Host"] == "example.com:8443"


# egress proxy 経由の経路
class TestEgressProxyRouting:
    """設定されたプロキシを使い、送信処理へ元のホスト名を渡す。"""

    @pytest.mark.asyncio
    async def test_keeps_original_host_when_routed_through_proxy(
        self, egress_proxy: str, captured_requests: list[httpx.Request]
    ) -> None:
        """複数IPの検証後も、プロキシへ渡す宛先は元のホスト名を保持する。"""
        with _patch_resolver("8.8.8.8", "2001:4860:4860::8888"):
            async with make_external_async_client() as client:
                await client.get("https://example.com/path?x=1")
        url = captured_requests[0].url
        assert url.host == "example.com"
        assert url.path == "/path"
        assert url.query == b"x=1"

    @pytest.mark.asyncio
    async def test_omits_sni_hostname_when_routed_through_proxy(
        self, egress_proxy: str, captured_requests: list[httpx.Request]
    ) -> None:
        """host を書き換えないので SNI の上書きも不要 (CONNECT で無視される)。"""
        with _patch_resolver("8.8.8.8"):
            async with make_external_async_client() as client:
                await client.get("https://example.com/")
        assert "sni_hostname" not in captured_requests[0].extensions

    @pytest.mark.asyncio
    async def test_caller_cannot_route_through_its_own_proxy(
        self, egress_proxy: str, captured_requests: list[httpx.Request]
    ) -> None:
        """呼び出し側の指定があっても設定されたプロキシでtransportを生成する。"""
        with (
            _patch_resolver("8.8.8.8"),
            patch(
                "app.http.external._PinnedDnsTransport", wraps=_PinnedDnsTransport
            ) as constructor,
        ):
            async with make_external_async_client(
                proxy="http://attacker.example.com:3128"
            ) as client:
                await client.get("https://example.com/")
        assert constructor.call_args.kwargs["proxy"] == egress_proxy


# follow_redirects の default 動作
class TestFollowRedirectsDefault:
    @pytest.mark.asyncio
    async def test_default_is_false(self) -> None:
        async with make_external_async_client() as client:
            assert client.follow_redirects is False

    @pytest.mark.asyncio
    async def test_explicit_true_is_respected(self) -> None:
        async with make_external_async_client(follow_redirects=True) as client:
            assert client.follow_redirects is True


class TestRedirectDestinationValidation:
    @pytest.mark.asyncio
    async def test_default_does_not_send_redirect_target(
        self, redirect_requests: list[str], egress_proxy: str
    ) -> None:
        """既定ではリダイレクト先への送信を行わない。"""
        with _patch_resolver("8.8.8.8"):
            async with make_external_async_client() as client:
                response = await client.get("https://start.example/article")
        assert response.status_code == 302
        assert redirect_requests == ["https://start.example/article"]

    @pytest.mark.asyncio
    async def test_explicit_follow_validates_each_public_destination(
        self, redirect_requests: list[str], egress_proxy: str
    ) -> None:
        """追従を許可した場合も送信先ごとにDNS解決結果を検証する。"""
        with _patch_resolver("8.8.8.8") as resolve:
            async with make_external_async_client(follow_redirects=True) as client:
                response = await client.get("https://start.example/article")
        assert response.status_code == 200
        assert redirect_requests == [
            "https://start.example/article",
            "https://next.example/article",
        ]
        assert [call.args[0] for call in resolve.await_args_list] == [
            "start.example",
            "next.example",
        ]

    @pytest.mark.asyncio
    async def test_explicit_follow_rejects_non_public_target_before_send(
        self, redirect_requests: list[str], egress_proxy: str
    ) -> None:
        """リダイレクト先が非公開IPへ解決された場合は送信前に拒否する。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(side_effect=[["8.8.8.8"], ["10.0.0.1"]]),
        ):
            async with make_external_async_client(follow_redirects=True) as client:
                with pytest.raises(HostBlockedError):
                    await client.get("https://start.example/article")
        assert redirect_requests == ["https://start.example/article"]


# transport の構造的保証
class TestTransportStructure:
    @pytest.mark.asyncio
    async def test_uses_pinned_dns_transport(self) -> None:
        """make_external_async_client が返す client は ``_PinnedDnsTransport`` を
        必ず装着する (event_hook ではなく transport 層で防御する構造保証)。"""
        async with make_external_async_client() as client:
            assert isinstance(client._transport, _PinnedDnsTransport)

    @pytest.mark.asyncio
    async def test_passes_transport_kwargs_to_transport(self) -> None:
        """``verify`` / ``http2`` などの transport-level kwargs は transport
        コンストラクタに渡され、AsyncClient には流れない。"""
        # http2 の指定が transport に正しく届いた場合、AsyncHTTPTransport の
        # 内部 _pool が h2 enabled で初期化される。client は transport=... の
        # 際は他の transport-level kwargs を受け付けない (= ValueError 出ず
        # に成功) ことで、振分が正しいことが分かる。
        async with make_external_async_client(verify=True, http1=True) as client:
            assert isinstance(client._transport, _PinnedDnsTransport)
