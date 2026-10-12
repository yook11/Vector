"""外部通信の最終宛先に許可するIPの共通方針。

非公開レンジの正本は同階層の ``non_public_ranges.json`` に置き、プロキシも参照する。
アプリは正本とPythonのIP判定のいずれかで非公開となるIPを拒否する。
DNS解決は ``destination_resolution``、送信時の適用は ``external`` が担当する。
"""

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
    """非公開レンジの正本。egress proxy の ACL と共有する。"""

    v4: tuple[str, ...]
    v6: tuple[str, ...]


def _load_non_public_ranges() -> _NonPublicRanges:
    raw = json.loads(_RANGES_FILE.read_text(encoding="utf-8"))
    return _NonPublicRanges(v4=tuple(raw["v4"]), v6=tuple(raw["v6"]))


NON_PUBLIC_RANGES = _load_non_public_ranges()

_NON_PUBLIC_V4 = tuple(ipaddress.ip_network(c) for c in NON_PUBLIC_RANGES.v4)
_NON_PUBLIC_V6 = tuple(ipaddress.ip_network(c) for c in NON_PUBLIC_RANGES.v6)


def _in_non_public_range(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """正本のレンジに含まれるか。``ipaddress`` のフラグが拾わない穴を埋める。

    フラグ側は Python のバージョンで更新されるため残す。両方の OR が判定になる。

    v4-mapped (``::ffff:0:0/96``) は埋め込み v4 を取り出して v4 レンジと照合する。
    正本の v6 側にこのレンジを置いていない (Squid は v4 として扱うため v4 レンジが
    覆う) ので、ここで展開しないと ``::ffff:100.64.0.1`` のような表記が素通りする。
    6to4 と Teredo はレンジ全体が ``is_private`` なのでフラグ側が拾う。
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    networks = _NON_PUBLIC_V4 if ip.version == 4 else _NON_PUBLIC_V6
    return any(ip in network for network in networks)


def _parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """括弧で囲まれたIPv6も読み、IPとして読めなければNoneを返す。"""
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


class HostBlockedReason(StrEnum):
    RESOLVED_TO_NON_PUBLIC = "resolved_to_non_public"
    NON_PUBLIC_IP_LITERAL = "non_public_ip_literal"


class HostBlockedError(ApplicationError):
    """IP直書きまたはDNS解決結果が外部通信の宛先方針で拒否された。"""

    def __init__(self, *, reason: HostBlockedReason, host: str, address: str) -> None:
        # 拒否したhostとIPは内部の宛先を示しうるため、説明文と診断情報へ入れない。
        super().__init__(
            "宛先が外部通信の方針で拒否されました", details={"reason": reason.value}
        )
        self.reason = reason
        self.host = host
        self.address = address


class PublicIpAddress:
    """SSRF 防御方針における「公開 IP アドレス」の値オブジェクト。

    Invariants:
    - 入力が IPv4 または IPv6 として valid
    - ``ipaddress`` のフラグ (private / loopback / link-local / reserved /
      multicast / unspecified) のいずれにも該当しない
    - ``non_public_ranges.json`` のどのレンジにも含まれない
    - 生成後は不変

    フラグだけでは漏れる例が実在する。CGN 空間 ``100.64.0.0/10`` は
    ``is_private`` も ``is_global`` も ``is_reserved`` も False、廃止された
    6to4 リレー ``192.88.99.0/24`` は ``is_global`` が True になる
    (Python 3.13.11 実測)。egress proxy 側は明示レンジで拒否しているので、
    正本を共有し、proxyの非公開レンジをアプリも拒否する。

    ``str(addr)`` で標準化された IP 表記が得られる
    (例: ``2001:db8::0001`` → ``2001:db8::1``)。

    文字列の判定には ``classify_ip_address`` を使う。

    Raises:
        ValueError: 公開IPではない値から生成しようとした。
    """

    __slots__ = ("_value",)

    def __init__(self, addr: str) -> None:
        ip = _parse_ip(addr)
        # 型が公開IPであることを保証するため、判定を経ない生成も同じ方針で拒否する。
        if ip is None or _is_non_public(ip):
            raise ValueError("公開IPアドレスではない値からは生成できません")
        # constructor 内では object.__setattr__ で _value を初期化し、
        # 以降は本クラスの __setattr__ でブロックする (frozen 風の挙動)。
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
    """IPとして読めるが、外部通信の宛先方針で非公開とするアドレスの標準表記。"""

    value: str


def classify_ip_address(text: str) -> PublicIpAddress | NonPublicIpAddress | None:
    """文字列がIPとして読めれば公開か非公開かを返し、読めなければNoneを返す。"""
    ip = _parse_ip(text)
    if ip is None:
        return None
    if _is_non_public(ip):
        return NonPublicIpAddress(str(ip))
    return PublicIpAddress(str(ip))
