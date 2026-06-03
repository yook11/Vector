"""ssrf_guard モジュールのユニットテスト。

PublicIpAddress (構造的検証) と ensure_host_is_public (DNS 解決検証) の
ポリシーを直接検証する。
"""

import socket
from unittest.mock import AsyncMock, patch

import pytest

from app.shared.security.ssrf_guard import (
    HostBlockedError,
    HostResolutionError,
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
    ensure_host_is_public,
)


# PublicIpAddress — Unit Tests
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
