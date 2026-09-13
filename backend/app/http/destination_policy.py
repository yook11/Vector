"""外部通信の最終宛先に許可するIPの共通方針。

非公開レンジの正本は同階層の ``non_public_ranges.json`` に置き、プロキシも参照する。
アプリは正本とPythonのIP判定のいずれかで非公開となるIPを拒否する。
DNS解決は ``destination_resolution``、送信時の適用は ``external`` が担当する。
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path

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


class NotAnIpAddressError(Exception):
    """文字列が IP アドレスとしてパースできない (DNS 名等)。"""


class NotAPublicIpError(Exception):
    """IP は valid だが SSRF 防御方針上 public ではない。

    ``ipaddress`` のフラグ (private / loopback / link-local / reserved /
    multicast / unspecified) のいずれか、または ``non_public_ranges.json`` の
    レンジに該当するアドレス。
    """


class HostBlockedError(Exception):
    """IP直書きまたはDNS解決結果が外部通信の宛先方針で拒否された。"""


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

    Raises:
        NotAnIpAddressError: 入力が IP として parse できない。
        NotAPublicIpError: IP だが内部レンジに属する。
    """

    __slots__ = ("_value",)

    def __init__(self, addr: str) -> None:
        candidate = addr.strip("[]")
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError as e:
            msg = f"not an IP address: {addr}"
            raise NotAnIpAddressError(msg) from e
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
            or _in_non_public_range(ip)
        ):
            msg = f"not a public IP: {addr}"
            raise NotAPublicIpError(msg)
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
