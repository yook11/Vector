"""PublicIpAddress単体の生成・形式不正・禁止IP・表記・同一性・不変性を検証する。"""

import ipaddress

import pytest

from app.http.destination_policy import (
    NON_PUBLIC_RANGES,
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
)


def _prohibited_range_boundary_cases():
    """JSONに列挙された範囲から両端の入力を作り、単一アドレスは一度だけ検証する。"""
    cases = []
    for cidr in (*NON_PUBLIC_RANGES.v4, *NON_PUBLIC_RANGES.v6):
        network = ipaddress.ip_network(cidr)
        cases.append(pytest.param(str(network[0]), id=f"{cidr}:start"))
        if network.num_addresses > 1:
            cases.append(pytest.param(str(network[-1]), id=f"{cidr}:end"))
    return cases


class TestPublicIpAddressConstruction:
    @pytest.mark.parametrize(
        "address",
        ["8.8.8.8", "1.1.1.1", "93.184.215.14", "8.8.8.0", "8.8.8.255"],
    )
    def test_accepts_public_ipv4(self, address: str) -> None:
        """許可されたIPv4から生成でき、各数値の有効範囲の両端も保持する。"""
        assert str(PublicIpAddress(address)) == address

    @pytest.mark.parametrize(
        "address",
        ["2001:4860:4860::8888", "2606:4700:4700::1111", "2001:4860:4860::ffff"],
    )
    def test_accepts_public_ipv6(self, address: str) -> None:
        """許可されたIPv6から生成でき、区画の最大値も保持する。"""
        assert str(PublicIpAddress(address)) == address

    def test_accepts_bracketed_ipv6(self) -> None:
        """一組の括弧で囲まれた公開IPv6から生成できる。"""
        assert str(PublicIpAddress("[2001:4860:4860::8888]")) == "2001:4860:4860::8888"


class TestPublicIpAddressInvalidSyntax:
    def test_rejects_dns_name(self) -> None:
        """ホスト名をIPアドレスとして生成しない。"""
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress("example.com")

    def test_rejects_empty_string(self) -> None:
        """空文字からIPアドレスを生成しない。"""
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress("")

    def test_rejects_non_address_text(self) -> None:
        """IP形式ではない文字列を方針拒否と区別して拒否する。"""
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress("not-an-ip")

    @pytest.mark.parametrize(
        "address",
        [
            pytest.param("8.8.8.-1", id="below-minimum"),
            pytest.param("8.8.8.256", id="above-maximum"),
        ],
    )
    def test_rejects_ipv4_octet_out_of_range(self, address: str) -> None:
        """IPv4の数値範囲を外れる入力からは生成できない。"""
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress(address)

    @pytest.mark.parametrize(
        "address",
        [
            pytest.param("8.8.8", id="missing-octet"),
            pytest.param("8.8.8.8.8", id="extra-octet"),
        ],
    )
    def test_rejects_ipv4_with_wrong_octet_count(self, address: str) -> None:
        """IPv4の区画数が不足・超過した入力からは生成できない。"""
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress(address)

    def test_rejects_ipv6_group_above_maximum(self) -> None:
        """IPv6の一区画がffffを超える入力からは生成できない。"""
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress("2001:4860:4860::10000")

    def test_rejects_ipv6_with_repeated_compression(self) -> None:
        """IPv6の省略記号を二度使う入力からは生成できない。"""
        with pytest.raises(NotAnIpAddressError):
            PublicIpAddress("2001::4860::8888")


class TestPublicIpAddressProhibitedAddresses:
    @pytest.mark.parametrize(
        "address",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.255.255",
        ],
    )
    def test_rejects_private_ipv4(self, address: str) -> None:
        """RFC1918のプライベートIPv4は形式が正しくても許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress(address)

    def test_rejects_ipv4_loopback(self) -> None:
        """IPv4ループバックは外部宛先として許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("127.0.0.1")

    def test_rejects_ipv4_link_local(self) -> None:
        """IPv4リンクローカルは外部宛先として許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("169.254.169.254")

    def test_rejects_ipv4_unspecified(self) -> None:
        """IPv4の未指定アドレスは外部宛先として許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("0.0.0.0")  # noqa: S104

    def test_rejects_ipv4_multicast(self) -> None:
        """IPv4マルチキャストは外部宛先として許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("224.0.0.1")

    def test_rejects_ipv6_loopback(self) -> None:
        """IPv6ループバックは外部宛先として許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("::1")

    def test_rejects_ipv6_link_local(self) -> None:
        """IPv6リンクローカルは外部宛先として許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("fe80::1")

    def test_rejects_ipv6_unique_local(self) -> None:
        """IPv6ユニークローカルは外部宛先として許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress("fc00::1")


class TestPublicIpAddressProhibitedRanges:
    def test_ipv4_ranges_are_configured(self) -> None:
        """IPv4の禁止レンジが空になり境界ケースが消えないことを確認する。"""
        assert NON_PUBLIC_RANGES.v4

    def test_ipv6_ranges_are_configured(self) -> None:
        """IPv6の禁止レンジが空になり境界ケースが消えないことを確認する。"""
        assert NON_PUBLIC_RANGES.v6

    @pytest.mark.parametrize("address", _prohibited_range_boundary_cases())
    def test_rejects_declared_range_boundary(self, address: str) -> None:
        """共通JSONで禁止した全レンジの両端からは生成できない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress(address)


class TestPublicIpAddressProhibitedRepresentations:
    @pytest.mark.parametrize(
        "address",
        [
            pytest.param("0:0:0:0:0:0:0:1", id="expanded-loopback"),
            pytest.param("[::1]", id="bracketed-loopback"),
        ],
    )
    def test_rejects_alternate_ipv6_loopback_notation(self, address: str) -> None:
        """IPv6ループバックは表記を変えても許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress(address)

    @pytest.mark.parametrize(
        "address", ["::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254"]
    )
    def test_rejects_mapped_non_public_ipv4(self, address: str) -> None:
        """非公開IPv4をIPv6へ埋め込んでも許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress(address)

    @pytest.mark.parametrize("address", ["::ffff:100.64.0.1", "::ffff:192.88.99.1"])
    def test_rejects_mapped_ipv4_from_prohibited_ranges(self, address: str) -> None:
        """共通JSONが禁止するIPv4はmapped表記でも許可しない。"""
        with pytest.raises(NotAPublicIpError):
            PublicIpAddress(address)


class TestPublicIpAddressIdentity:
    def test_normalizes_ipv6_string(self) -> None:
        """IPv6の非短縮形を標準の短縮表記で返す。"""
        assert (
            str(PublicIpAddress("2001:4860:4860:0:0:0:0:8888"))
            == "2001:4860:4860::8888"
        )

    def test_repr_identifies_value_type(self) -> None:
        """reprからIPの値型と保持しているアドレスが分かる。"""
        assert repr(PublicIpAddress("8.8.8.8")) == "PublicIpAddress('8.8.8.8')"

    def test_same_ipv4_addresses_compare_equal(self) -> None:
        """同じIPv4から生成した値は等しい。"""
        assert PublicIpAddress("8.8.8.8") == PublicIpAddress("8.8.8.8")

    def test_different_addresses_compare_unequal(self) -> None:
        """異なるIPから生成した値を同一視しない。"""
        assert PublicIpAddress("8.8.8.8") != PublicIpAddress("1.1.1.1")

    def test_address_is_not_equal_to_plain_string(self) -> None:
        """文字列が同じでも検証済みのIP型と単なる文字列は同一視しない。"""
        assert PublicIpAddress("8.8.8.8") != "8.8.8.8"

    @pytest.mark.parametrize(
        "address", ["2001:4860:4860:0:0:0:0:8888", "[2001:4860:4860::8888]"]
    )
    def test_equivalent_ipv6_notations_compare_equal(self, address: str) -> None:
        """同じIPv6の異なる表記から生成した値は等しい。"""
        assert PublicIpAddress(address) == PublicIpAddress("2001:4860:4860::8888")

    def test_equal_ipv4_addresses_have_equal_hashes(self) -> None:
        """同じIPv4の値は同じハッシュを持つ。"""
        assert hash(PublicIpAddress("8.8.8.8")) == hash(PublicIpAddress("8.8.8.8"))

    def test_equal_ipv6_notations_have_equal_hashes(self) -> None:
        """表記が異なっても等しいIPv6の値は同じハッシュを持つ。"""
        assert hash(PublicIpAddress("2001:4860:4860:0:0:0:0:8888")) == hash(
            PublicIpAddress("2001:4860:4860::8888")
        )

    def test_equal_addresses_share_one_set_entry(self) -> None:
        """同じIPから生成した値は集合内で重複しない。"""
        assert len({PublicIpAddress("8.8.8.8"), PublicIpAddress("8.8.8.8")}) == 1


class TestPublicIpAddressImmutability:
    def test_cannot_replace_address(self) -> None:
        """生成後のIPを別の値へ変更できない。"""
        addr = PublicIpAddress("8.8.8.8")
        with pytest.raises(AttributeError):
            addr._value = "1.1.1.1"  # type: ignore[misc]
