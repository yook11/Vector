"""実DB・外部通信なしでIP判定と、公開IPの値型の表記・同一性・不変性を検証する。"""

import pytest

from app.http.destination_policy import (
    NON_PUBLIC_RANGES,
    NonPublicIpAddress,
    PublicIpAddress,
    classify_ip_address,
)


def _prohibited_range_boundary_cases():
    """JSONに列挙された範囲から両端の入力を作り、単一アドレスは一度だけ検証する。"""
    cases = []
    for network in (*NON_PUBLIC_RANGES.v4, *NON_PUBLIC_RANGES.v6):
        cases.append(pytest.param(str(network[0]), id=f"{network}:start"))
        if network.num_addresses > 1:
            cases.append(pytest.param(str(network[-1]), id=f"{network}:end"))
    return cases


class TestPublicIpAddressClassification:
    @pytest.mark.parametrize(
        "address",
        ["8.8.8.8", "1.1.1.1", "93.184.215.14", "8.8.8.0", "8.8.8.255"],
    )
    def test_classifies_public_ipv4(self, address: str) -> None:
        """許可されたIPv4は公開IPと判定し、各数値の有効範囲の両端も保持する。"""
        result = classify_ip_address(address)
        assert (type(result), str(result)) == (PublicIpAddress, address)

    @pytest.mark.parametrize(
        "address",
        ["2001:4860:4860::8888", "2606:4700:4700::1111", "2001:4860:4860::ffff"],
    )
    def test_classifies_public_ipv6(self, address: str) -> None:
        """許可されたIPv6は公開IPと判定し、区画の最大値も保持する。"""
        result = classify_ip_address(address)
        assert (type(result), str(result)) == (PublicIpAddress, address)

    def test_classifies_bracketed_ipv6(self) -> None:
        """一組の括弧で囲まれた公開IPv6は括弧を外した公開IPと判定する。"""
        result = classify_ip_address("[2001:4860:4860::8888]")
        assert (type(result), str(result)) == (PublicIpAddress, "2001:4860:4860::8888")


class TestNotAnIpAddress:
    def test_dns_name_is_not_an_ip(self) -> None:
        """ホスト名はIPではないと判定する。"""
        assert classify_ip_address("example.com") is None

    def test_empty_string_is_not_an_ip(self) -> None:
        """空文字はIPではないと判定する。"""
        assert classify_ip_address("") is None

    def test_non_address_text_is_not_an_ip(self) -> None:
        """IP形式ではない文字列は、非公開IPと区別してIPではないと判定する。"""
        assert classify_ip_address("not-an-ip") is None

    @pytest.mark.parametrize(
        "address",
        [
            pytest.param("8.8.8.-1", id="below-minimum"),
            pytest.param("8.8.8.256", id="above-maximum"),
        ],
    )
    def test_ipv4_octet_out_of_range_is_not_an_ip(self, address: str) -> None:
        """IPv4の数値範囲を外れる入力はIPではないと判定する。"""
        assert classify_ip_address(address) is None

    @pytest.mark.parametrize(
        "address",
        [
            pytest.param("8.8.8", id="missing-octet"),
            pytest.param("8.8.8.8.8", id="extra-octet"),
        ],
    )
    def test_ipv4_with_wrong_octet_count_is_not_an_ip(self, address: str) -> None:
        """IPv4の区画数が不足・超過した入力はIPではないと判定する。"""
        assert classify_ip_address(address) is None

    def test_ipv6_group_above_maximum_is_not_an_ip(self) -> None:
        """IPv6の一区画がffffを超える入力はIPではないと判定する。"""
        assert classify_ip_address("2001:4860:4860::10000") is None

    def test_ipv6_with_repeated_compression_is_not_an_ip(self) -> None:
        """IPv6の省略記号を二度使う入力はIPではないと判定する。"""
        assert classify_ip_address("2001::4860::8888") is None


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
    def test_classifies_private_ipv4_as_non_public(self, address: str) -> None:
        """RFC1918のプライベートIPv4は形式が正しくても非公開IPと判定する。"""
        assert type(classify_ip_address(address)) is NonPublicIpAddress

    def test_classifies_ipv4_loopback_as_non_public(self) -> None:
        """IPv4ループバックは外部宛先として許可しない非公開IPと判定する。"""
        assert classify_ip_address("127.0.0.1") == NonPublicIpAddress("127.0.0.1")

    def test_classifies_ipv4_link_local_as_non_public(self) -> None:
        """IPv4リンクローカルは外部宛先として許可しない非公開IPと判定する。"""
        assert classify_ip_address("169.254.169.254") == NonPublicIpAddress(
            "169.254.169.254"
        )

    def test_classifies_ipv4_unspecified_as_non_public(self) -> None:
        """IPv4の未指定アドレスは外部宛先として許可しない非公開IPと判定する。"""
        assert classify_ip_address("0.0.0.0") == NonPublicIpAddress("0.0.0.0")  # noqa: S104

    def test_classifies_ipv4_multicast_as_non_public(self) -> None:
        """IPv4マルチキャストは外部宛先として許可しない非公開IPと判定する。"""
        assert classify_ip_address("224.0.0.1") == NonPublicIpAddress("224.0.0.1")

    def test_classifies_ipv6_loopback_as_non_public(self) -> None:
        """IPv6ループバックは外部宛先として許可しない非公開IPと判定する。"""
        assert classify_ip_address("::1") == NonPublicIpAddress("::1")

    def test_classifies_ipv6_link_local_as_non_public(self) -> None:
        """IPv6リンクローカルは外部宛先として許可しない非公開IPと判定する。"""
        assert classify_ip_address("fe80::1") == NonPublicIpAddress("fe80::1")

    def test_classifies_ipv6_unique_local_as_non_public(self) -> None:
        """IPv6ユニークローカルは外部宛先として許可しない非公開IPと判定する。"""
        assert classify_ip_address("fc00::1") == NonPublicIpAddress("fc00::1")


class TestPublicIpAddressProhibitedRanges:
    def test_ipv4_ranges_are_configured(self) -> None:
        """IPv4の禁止レンジが空になり境界ケースが消えないことを確認する。"""
        assert NON_PUBLIC_RANGES.v4

    def test_ipv6_ranges_are_configured(self) -> None:
        """IPv6の禁止レンジが空になり境界ケースが消えないことを確認する。"""
        assert NON_PUBLIC_RANGES.v6

    @pytest.mark.parametrize("address", _prohibited_range_boundary_cases())
    def test_classifies_declared_range_boundary_as_non_public(
        self, address: str
    ) -> None:
        """共通JSONで禁止した全レンジの両端は非公開IPと判定する。"""
        assert type(classify_ip_address(address)) is NonPublicIpAddress


class TestPublicIpAddressProhibitedRepresentations:
    @pytest.mark.parametrize(
        "address",
        [
            pytest.param("0:0:0:0:0:0:0:1", id="expanded-loopback"),
            pytest.param("[::1]", id="bracketed-loopback"),
        ],
    )
    def test_classifies_alternate_ipv6_loopback_notation(self, address: str) -> None:
        """IPv6ループバックは表記を変えても標準表記の非公開IPと判定する。"""
        assert classify_ip_address(address) == NonPublicIpAddress("::1")

    @pytest.mark.parametrize(
        "address", ["::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254"]
    )
    def test_classifies_mapped_non_public_ipv4(self, address: str) -> None:
        """非公開IPv4をIPv6へ埋め込んでも非公開IPと判定する。"""
        assert type(classify_ip_address(address)) is NonPublicIpAddress

    @pytest.mark.parametrize("address", ["::ffff:100.64.0.1", "::ffff:192.88.99.1"])
    def test_classifies_mapped_ipv4_from_prohibited_ranges(self, address: str) -> None:
        """共通JSONが禁止するIPv4はmapped表記でも非公開IPと判定する。"""
        assert type(classify_ip_address(address)) is NonPublicIpAddress


class TestPublicIpAddressConstructionGuard:
    @pytest.mark.parametrize("address", ["10.0.0.1", "example.com"])
    def test_rejects_values_other_than_public_ip(self, address: str) -> None:
        """判定を経ずに公開IPの型を作ろうとしても、公開IP以外からは生成できない。"""
        with pytest.raises(ValueError):
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
