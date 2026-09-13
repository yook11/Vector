"""``make_external_async_client`` のユニットテスト。

SSRF 検証 + DNS rebind 防御を担う ``_PinnedDnsTransport`` の挙動、
``follow_redirects`` の default、transport-level kwargs の振分を検証する。
親 class ``httpx.AsyncHTTPTransport.handle_async_request`` を monkeypatch で
short-circuit して、実 HTTP は出さずに pin された Request を観察する。
"""

from __future__ import annotations

from pathlib import Path
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


# Transport ベースの SSRF 検証
class TestSsrfValidation:
    @pytest.mark.asyncio
    async def test_blocks_request_to_private_host(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        with _patch_resolver("10.0.0.1"):
            async with make_external_async_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://internal.example/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_blocks_request_to_loopback(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        with _patch_resolver("127.0.0.1"):
            async with make_external_async_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://localhost-alias.example.com/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_allows_request_to_public_host(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        with _patch_resolver("8.8.8.8"):
            async with make_external_async_client() as client:
                resp = await client.get("https://example.com/")
        assert resp.status_code == 200
        assert len(captured_requests) == 1

    @pytest.mark.asyncio
    async def test_propagates_host_resolution_error(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        import socket

        with _patch_resolver(socket.gaierror("name unknown")):
            async with make_external_async_client() as client:
                with pytest.raises(HostResolutionError):
                    await client.get("https://nonexistent.invalid/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_blocks_private_ip_literal(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """private IP literal を直接渡された場合も transport が拒否する
        (defense-in-depth: SafeUrl で弾く前提だが二重保証)。"""
        async with make_external_async_client() as client:
            with pytest.raises(HostBlockedError):
                await client.get("http://10.0.0.1/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_passes_public_ip_literal_without_resolve(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """public IP literal は DNS resolve せず通過。"""
        with patch(
            "app.http.destination_resolution._resolve_host",
            new=AsyncMock(side_effect=AssertionError("must not resolve")),
        ):
            async with make_external_async_client() as client:
                resp = await client.get("https://8.8.8.8/")
        assert resp.status_code == 200
        assert str(captured_requests[0].url) == "https://8.8.8.8/"


# DNS rebind 防御
class TestDnsRebindResistance:
    """validate と connect の間で DNS が切り替わっても
    TCP 接続は validate 済の最初の IP に pin される (TOCTOU 不成立)。"""

    @pytest.mark.asyncio
    async def test_pins_to_first_resolved_ip(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """``_resolve_host`` が複数回呼ばれても、TCP 接続先は 1 回目の解決
        結果のみに依存する。
        """
        call_count = 0

        async def fake_resolve(host: str) -> list[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ["8.8.8.8"]
            return ["172.20.0.5"]  # 2nd resolve で internal を返す rebind 攻撃

        with patch(
            "app.http.destination_resolution._resolve_host", side_effect=fake_resolve
        ):
            async with httpx.AsyncClient(transport=_PinnedDnsTransport()) as client:
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
            async with httpx.AsyncClient(transport=_PinnedDnsTransport()) as client:
                await client.get("https://example.com/path")
        assert captured_requests[0].headers["Host"] == "example.com"

    @pytest.mark.asyncio
    async def test_sets_sni_hostname_extension_for_tls_verify(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """``extensions["sni_hostname"]`` に元 host を設定し、TLS server_hostname
        が IP に書換わらないことを保証 (cert verify pass 担保)。"""
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
    """proxy を通す構成では接続先を書き換えない。

    httpcore は CONNECT トンネルに ``sni_hostname`` を渡さないため、host を IP へ
    書き換えると証明書の hostname 検証が壊れる。DNS rebind に対する防御はこの構成
    でだけ proxy 側の非公開宛先 deny に移る。公開性の検証そのものは常に通す。
    proxy 無し構成の pin は ``TestDnsRebindResistance`` が所有する。

    経路は ``settings.egress_proxy_url`` だけが決める。kwargs から transport へ
    proxy が渡ったかは host が書き換わらないことで観察する (proxy pool の構築自体は
    httpx の責務)。
    """

    @pytest.mark.asyncio
    async def test_keeps_original_host_when_routed_through_proxy(
        self, egress_proxy: str, captured_requests: list[httpx.Request]
    ) -> None:
        with _patch_resolver("8.8.8.8"):
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
    async def test_still_blocks_private_host_when_routed_through_proxy(
        self, egress_proxy: str, captured_requests: list[httpx.Request]
    ) -> None:
        """公開性の検証は proxy 構成でも通る (proxy の deny に到達する前に落とす)。"""
        with _patch_resolver("10.0.0.1"):
            async with make_external_async_client() as client:
                with pytest.raises(HostBlockedError):
                    await client.get("http://internal.example/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_still_blocks_private_ip_literal_when_routed_through_proxy(
        self, egress_proxy: str, captured_requests: list[httpx.Request]
    ) -> None:
        async with make_external_async_client() as client:
            with pytest.raises(HostBlockedError):
                await client.get("http://10.0.0.1/")
        assert captured_requests == []

    @pytest.mark.asyncio
    async def test_caller_cannot_route_through_its_own_proxy(
        self, captured_requests: list[httpx.Request]
    ) -> None:
        """呼び出し側の ``proxy=`` は経路を変えない (egress は factory が所有する)。

        環境設定のproxyを維持し、呼び出し側の指定を採用しない。
        """
        with _patch_resolver("8.8.8.8"):
            async with make_external_async_client(
                proxy="http://attacker.example.com:3128"
            ) as client:
                await client.get("https://example.com/")
        assert captured_requests[0].url.host == "example.com"


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


_SQUID_CONF_TEMPLATE = (
    Path(__file__).parents[3] / "infra" / "aws" / "templates" / "squid.conf.tftpl"
)
_DENY_NON_PUBLIC = "http_access deny to_private"


def _squid_directives() -> list[str]:
    """コメントと Terraform の制御行を落とした Squid ディレクティブ列 (評価順)。"""
    return [
        stripped
        for line in _SQUID_CONF_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if (stripped := line.strip()) and not stripped.startswith(("#", "%{"))
    ]


class TestEgressProxyDenyContract:
    """app が IP pin を手放す根拠が proxy 側に実在することを固定する。

    proxy を経由する構成では DNS rebind 防御の最終責任が Squid の
    ``http_access deny to_private`` に移る (``http.external`` の module docstring)。
    レンジ定義の一致は ``TestNonPublicRangeParity`` が見るが、**拒否そのものが
    conf に書かれているか** は誰も見ていなかった。この行を消してもレンジは一致する。
    """

    def test_template_is_readable(self) -> None:
        """正本の場所がずれたら黙って緑にならず、ここで落ちる。"""
        assert _SQUID_CONF_TEMPLATE.is_file()

    def test_denies_non_public_destinations(self) -> None:
        assert _DENY_NON_PUBLIC in _squid_directives()

    @pytest.mark.parametrize("variable", ["private_v4_ranges", "private_v6_ranges"])
    def test_deny_covers_range_source(self, variable: str) -> None:
        """acl が正本の v4 / v6 双方を参照する (片方の列挙漏れは穴になる)。"""
        acl = " ".join(
            d for d in _squid_directives() if d.startswith("acl to_private ")
        )
        assert variable in acl

    def test_deny_precedes_every_allow(self) -> None:
        """Squid は上から評価するので、allow より後ろに置いた deny は死ぬ。"""
        directives = _squid_directives()
        first_allow = next(
            i for i, d in enumerate(directives) if d.startswith("http_access allow")
        )
        assert directives.index(_DENY_NON_PUBLIC) < first_allow
