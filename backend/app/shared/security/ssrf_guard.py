"""SSRF 防御の SSoT。

「どの IP 範囲を public とみなすか」「ホスト名を実フェッチして良いか」の
アクセスポリシーを 1 箇所に集約する。fetch 機構 (httpx クライアントの
リトライ可否など) は知らない: 政策専用例外を出し、呼び出し側が文脈に
応じて翻訳する。

検証ロジックは ``PublicIpAddress`` の constructor に押し込み、
「型が存在する = 検証済み」を構造的に保証する。``is_blocked_ip`` の
ような直接判定関数は public API として公開しない (VO 構築が SSoT)。

政策例外 (``HostBlockedError`` / ``HostResolutionError``) の現役の翻訳先は
``article_acquisition/tools/http_error_translation.py`` の
``translate_fetch_exception`` が持つ。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket


class NotAnIpAddressError(Exception):
    """文字列が IP アドレスとしてパースできない (DNS 名等)。"""


class NotAPublicIpError(Exception):
    """IP は valid だが SSRF 防御方針上 public ではない。

    private / loopback / link-local / reserved / multicast / unspecified
    のいずれかに該当するアドレス。
    """


class HostBlockedError(Exception):
    """ホスト名の DNS 解決結果がアクセスポリシー上拒否された。"""


class HostResolutionError(Exception):
    """ホスト名の DNS 解決自体に失敗した。"""


class PublicIpAddress:
    """SSRF 防御方針における「公開 IP アドレス」の値オブジェクト。

    Invariants:
    - 入力が IPv4 または IPv6 として valid
    - private / loopback / link-local / reserved / multicast / unspecified
      のいずれにも該当しない
    - 生成後は不変

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


async def _resolve_host(host: str) -> list[str]:
    """ホスト名を DNS 解決し、IP 文字列のリストを返す。

    テスト時は本関数を patch することで DNS をモックできる。
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


async def ensure_host_is_public(host: str) -> tuple[PublicIpAddress, ...]:
    """ホスト名を DNS 解決し、全アドレスが ``PublicIpAddress`` であることを保証する。

    docker compose のサービス名 (``backend``, ``db``, ...) や、A レコードが
    プライベート IP に向いている悪意あるドメインを実フェッチ前に弾く。

    Returns:
        検証済みアドレスのタプル (1 件以上)。

    Raises:
        HostBlockedError: いずれかの解決結果が public でない。
        HostResolutionError: DNS 解決に失敗した。

    Note:
        本関数単独では DNS rebinding (本関数の解決結果と httpx 側の解決結果が
        ずれる TOCTOU) を防げない。``app/shared/security/safe_http.py`` の
        ``_PinnedDnsTransport`` がここで返した最初の IP に TCP 接続を pin し、
        TOCTOU を構造的に閉塞する。本関数は IP allowlist の判定だけを担う。
    """
    try:
        resolved = await _resolve_host(host)
    except socket.gaierror as e:
        msg = f"DNS resolution failed for host: {host}: {e}"
        raise HostResolutionError(msg) from e

    addrs: list[PublicIpAddress] = []
    for addr in resolved:
        try:
            addrs.append(PublicIpAddress(addr))
        except NotAPublicIpError as e:
            msg = f"host resolves to non-public address: {host} -> {addr}"
            raise HostBlockedError(msg) from e
        except NotAnIpAddressError:
            # getaddrinfo は IP を返すので通常ここには来ないが defense-in-depth
            continue
    return tuple(addrs)
