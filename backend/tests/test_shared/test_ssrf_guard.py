"""ssrf_guard モジュールのユニットテスト。

PublicIpAddress (構造的検証) と ensure_host_is_public (DNS 解決検証) の
ポリシーを直接検証する。
"""

import ipaddress
import socket
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.shared.security.ssrf_guard import (
    NON_PUBLIC_RANGES,
    HostBlockedError,
    HostResolutionError,
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
    ensure_host_is_public,
)


class TestPublicIpAddressAccepts:
    def test_accepts_ipv4_public(self) -> None:
        addr = PublicIpAddress("8.8.8.8")
        assert str(addr) == "8.8.8.8"

    def test_accepts_ipv6_public(self) -> None:
        addr = PublicIpAddress("2001:4860:4860::8888")
        assert str(addr) == "2001:4860:4860::8888"

    def test_accepts_ipv6_with_brackets(self) -> None:
        addr = PublicIpAddress("[2001:4860:4860::8888]")
        assert str(addr) == "2001:4860:4860::8888"


class TestPublicIpAddressRejectsNotIp:
    def test_rejects_dns_name(self) -> None:
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress("example.com")

    def test_rejects_empty(self) -> None:
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress("")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress("not-an-ip")


class TestPublicIpAddressRejectsNonPublic:
    @pytest.mark.parametrize(
        "addr",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.255.255",
        ],
    )
    def test_rejects_ipv4_private_rfc1918(self, addr: str) -> None:
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress(addr)

    def test_rejects_ipv4_loopback(self) -> None:
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("127.0.0.1")

    def test_rejects_ipv4_link_local(self) -> None:
        # 169.254.0.0/16 は AWS/GCP メタデータ等で典型的な攻撃対象
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("169.254.169.254")

    def test_rejects_ipv4_unspecified(self) -> None:
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("0.0.0.0")  # noqa: S104

    def test_rejects_ipv4_multicast(self) -> None:
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("224.0.0.1")

    def test_rejects_ipv6_loopback(self) -> None:
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("::1")

    def test_rejects_ipv6_loopback_with_brackets(self) -> None:
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("[::1]")

    def test_rejects_ipv6_link_local(self) -> None:
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("fe80::1")

    def test_rejects_ipv6_unique_local(self) -> None:
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("fc00::1")


class TestPublicIpAddressIdentity:
    def test_str_normalises_form(self) -> None:
        # 公開アドレスの IPv6 を非短縮形で渡し、ipaddress による正規化を確認する
        addr = PublicIpAddress("2001:4860:4860:0:0:0:0:8888")
        assert str(addr) == "2001:4860:4860::8888"

    def test_repr(self) -> None:
        assert repr(PublicIpAddress("8.8.8.8")) == "PublicIpAddress('8.8.8.8')"

    def test_equality(self) -> None:
        assert PublicIpAddress("8.8.8.8") == PublicIpAddress("8.8.8.8")
        assert PublicIpAddress("8.8.8.8") != PublicIpAddress("1.1.1.1")

    def test_equality_different_type(self) -> None:
        assert PublicIpAddress("8.8.8.8") != "8.8.8.8"

    def test_hash_consistency(self) -> None:
        a = PublicIpAddress("8.8.8.8")
        b = PublicIpAddress("8.8.8.8")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_immutable(self) -> None:
        addr = PublicIpAddress("8.8.8.8")
        with pytest.raises(AttributeError):
            addr._value = "1.1.1.1"  # type: ignore[misc]


# ensure_host_is_public — DNS Resolution Tests
def _patch_resolver(*addrs: str | Exception):
    """``_resolve_host`` を patch し、指定の戻り値/例外を返すようにする。"""
    if len(addrs) == 1 and isinstance(addrs[0], Exception):
        return patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(side_effect=addrs[0]),
        )
    return patch(
        "app.shared.security.ssrf_guard._resolve_host",
        new=AsyncMock(return_value=list(addrs)),
    )


class TestEnsureHostIsPublic:
    @pytest.mark.asyncio
    async def test_accepts_host_resolving_to_public_ipv4(self) -> None:
        with _patch_resolver("8.8.8.8"):
            addrs = await ensure_host_is_public("dns.google")
        assert len(addrs) == 1
        assert str(addrs[0]) == "8.8.8.8"

    @pytest.mark.asyncio
    async def test_accepts_host_resolving_to_public_ipv6(self) -> None:
        with _patch_resolver("2001:4860:4860::8888"):
            addrs = await ensure_host_is_public("dns.google")
        assert len(addrs) == 1
        assert str(addrs[0]) == "2001:4860:4860::8888"

    @pytest.mark.asyncio
    async def test_rejects_host_resolving_to_private(self) -> None:
        # docker compose の `backend` のようなサービス名のシナリオ
        with _patch_resolver("172.18.0.5"):
            with pytest.raises(HostBlockedError, match="172.18.0.5"):
                await ensure_host_is_public("backend")

    @pytest.mark.asyncio
    async def test_rejects_host_resolving_to_link_local(self) -> None:
        # クラウドメタデータエンドポイントのシナリオ (169.254.169.254 への A レコード)
        with _patch_resolver("169.254.169.254"):
            with pytest.raises(HostBlockedError, match="169.254.169.254"):
                await ensure_host_is_public("metadata-attack.example.com")

    @pytest.mark.asyncio
    async def test_rejects_host_resolving_to_loopback(self) -> None:
        with _patch_resolver("127.0.0.1"):
            with pytest.raises(HostBlockedError, match="127.0.0.1"):
                await ensure_host_is_public("localhost-alias.example.com")

    @pytest.mark.asyncio
    async def test_rejects_when_any_resolved_address_is_private(self) -> None:
        # マルチホーム: public + private が混在 → 全件 public でないと NG
        with _patch_resolver("8.8.8.8", "10.0.0.1"):
            with pytest.raises(HostBlockedError, match="10.0.0.1"):
                await ensure_host_is_public("multihomed.example.com")

    @pytest.mark.asyncio
    async def test_raises_resolution_error_on_dns_failure(self) -> None:
        with _patch_resolver(socket.gaierror("Name or service not known")):
            with pytest.raises(HostResolutionError, match="DNS resolution failed"):
                await ensure_host_is_public("nonexistent.invalid")


class TestNonPublicRangeParity:
    """レンジ正本と ``PublicIpAddress`` の判定が一致することを固定する。

    同じ「公開ではない宛先」の定義が app (本 VO) と egress proxy (Squid の
    ``acl to_private``) の 2 箇所で使われる。Squid はレンジの明示列挙しか
    書けないため、正本を ``non_public_ranges.json`` に置いて双方が読む。

    ここで守る不変条件は **proxy が拒否するものは app も必ず拒否する** こと。
    逆向き (app の方が厳しい) は許す: app が先に落とすので外へ出ず、
    proxy の 403 が ``ProxyError`` として通信障害に誤分類される事故が起きない。
    """

    @staticmethod
    def _representatives(cidr: str) -> list[str]:
        """レンジの下端と上端を返す。境界のずれを検出するため両端を見る。"""
        net = ipaddress.ip_network(cidr)
        if net.num_addresses == 1:
            return [str(net[0])]
        return [str(net[0]), str(net[-1])]

    def test_ranges_file_is_loadable(self) -> None:
        assert NON_PUBLIC_RANGES.v4
        assert NON_PUBLIC_RANGES.v6

    def test_every_declared_range_is_rejected(self) -> None:
        """正本の全レンジについて、両端が public として通らないこと。"""
        leaked: list[str] = []
        for cidr in [*NON_PUBLIC_RANGES.v4, *NON_PUBLIC_RANGES.v6]:
            for addr in self._representatives(cidr):
                try:
                    PublicIpAddress(addr)
                except NotAPublicIpError:
                    continue
                leaked.append(f"{cidr} -> {addr}")
        assert not leaked, (
            "proxy が拒否するのに app が public として通すレンジがある "
            f"(proxy の deny ⊆ app の deny が破れている): {leaked}"
        )

    @pytest.mark.parametrize(
        "addr",
        [
            # フラグが埋め込み v4 の意味論で拾う分 (Python 3.12.4 以降の挙動)。
            "::ffff:10.0.0.1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            # フラグが拾わず、正本のレンジでしか塞げない分。
            "::ffff:100.64.0.1",
            "::ffff:192.88.99.1",
        ],
    )
    def test_rejects_ipv4_mapped_form(self, addr: str) -> None:
        """v4-mapped 形式でも同じ判定になること。

        正本の v6 リストから ``::ffff:0:0/96`` を意図的に外している
        (Squid は v4 として扱うので v4 レンジが覆う) ため、app 側は
        埋め込み v4 を取り出して v4 レンジと突き合わせる必要がある。
        外すと ``https://[::ffff:100.64.0.1]/`` のような URL literal が素通りする。
        """
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress(addr)

    def test_public_addresses_still_pass(self) -> None:
        """レンジを足しすぎて正当な宛先を塞いでいないこと。"""
        for addr in ("8.8.8.8", "1.1.1.1", "93.184.215.14", "2606:4700:4700::1111"):
            assert str(PublicIpAddress(addr)) == str(ipaddress.ip_address(addr))


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
