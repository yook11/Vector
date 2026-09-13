"""外部通信の公開IP方針と非公開レンジ正本を検証する。"""

import ipaddress

import pytest

from app.http.destination_policy import (
    NON_PUBLIC_RANGES,
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
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


class TestNonPublicRangeParity:
    """レンジ正本に含まれる宛先をアプリも拒否することを固定する。

    同じ「公開ではない宛先」の定義が app (本 VO) と egress proxy (Squid の
    ``acl to_private``) の 2 箇所で使われる。Squid はレンジの明示列挙しか
    書けないため、正本を ``non_public_ranges.json`` に置いて双方が読む。

    ここで守る条件は、同じIPに対して **proxyの非公開レンジ拒否 ⊆ appの拒否**
    となることで、両者のDNS解決結果の一致やドメイン・ポート制限は保証しない。
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
