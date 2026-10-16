"""PythonのIP判定とプロキシと共有するJSONの拒否レンジを使い、外部通信の宛先IPを分類する。"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.shared.errors import ApplicationError

_RANGES_FILE = Path(__file__).with_name("non_public_ranges.json")


@dataclass(frozen=True, slots=True)
class _NonPublicRanges:
    """JSONから読み込んだ非公開IPレンジをIPv4・IPv6別に保持する。"""

    v4: tuple[ipaddress.IPv4Network, ...]
    v6: tuple[ipaddress.IPv6Network, ...]


def _load_non_public_ranges() -> _NonPublicRanges:
    raw = json.loads(_RANGES_FILE.read_text(encoding="utf-8"))
    return _NonPublicRanges(
        v4=tuple(ipaddress.IPv4Network(c) for c in raw["v4"]),
        v6=tuple(ipaddress.IPv6Network(c) for c in raw["v6"]),
    )


NON_PUBLIC_RANGES = _load_non_public_ranges()


def _in_non_public_range(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """外部通信の宛先として禁止しているIP範囲に含まれるかを判定し、
    IPv4-mapped IPv6は表記による判定漏れを防ぐためIPv4を取り出して照合する。
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    networks = NON_PUBLIC_RANGES.v4 if ip.version == 4 else NON_PUBLIC_RANGES.v6
    return any(ip in network for network in networks)


def _parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """両端の角括弧を除いてIPとして解析し、解析できなければNoneを返す。"""
    try:
        return ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return None


def _is_non_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or _in_non_public_range(ip)
    )


class DestinationRejectedReason(StrEnum):
    RESOLVED_TO_NON_PUBLIC = "resolved_to_non_public"
    NON_PUBLIC_IP_LITERAL = "non_public_ip_literal"


class DestinationRejectedError(ApplicationError):
    """直接指定またはDNS解決で得たIPが、外部通信の拒否対象だったことを表す例外。"""

    def __init__(
        self, *, reason: DestinationRejectedReason, host: str, address: str
    ) -> None:
        # 拒否したhostとIPは内部の宛先を示しうるため、説明文と診断情報へ入れない。
        super().__init__(
            "宛先が外部通信の方針で拒否されました", details={"reason": reason.value}
        )
        self.reason = reason
        self.host = host
        self.address = address


class PublicIpAddress:
    """外部通信で許可するIPだけを受け付け、正規化した文字列を変更不可で保持する。"""

    __slots__ = ("_value",)

    def __init__(self, addr: str) -> None:
        ip = _parse_ip(addr)
        # 型が公開IPであることを保証するため、判定を経ない生成も同じ方針で拒否する。
        if ip is None or _is_non_public(ip):
            raise ValueError("公開IPアドレスではない値からは生成できません")
        # constructor 内では object.__setattr__ で _value を初期化し、
        # 以降は本クラスの __setattr__ でブロックする 。
        object.__setattr__(self, "_value", str(ip))

    def __setattr__(self, name: str, value: object) -> None:
        msg = f"PublicIpAddress is immutable, cannot set {name!r}"
        raise AttributeError(msg)

    def __delattr__(self, name: str) -> None:
        msg = f"PublicIpAddress is immutable, cannot delete {name!r}"
        raise AttributeError(msg)

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"PublicIpAddress({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PublicIpAddress):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True, slots=True)
class NonPublicIpAddress:
    """外部通信の拒否対象と判定されたIPアドレスを保持する。"""

    value: str


def classify_ip_address(text: str) -> PublicIpAddress | NonPublicIpAddress | None:
    """文字列を公開IP・非公開IPに分類し、IPとして解析できなければNoneを返す。"""
    ip = _parse_ip(text)
    if ip is None:
        return None
    if _is_non_public(ip):
        return NonPublicIpAddress(str(ip))
    return PublicIpAddress(str(ip))
