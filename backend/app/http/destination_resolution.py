"""ホスト名をDNS解決し、外部通信の共通宛先方針を適用する。"""

from __future__ import annotations

import asyncio
import socket

from app.http.destination_policy import (
    HostBlockedError,
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
)


class HostResolutionError(Exception):
    """名前解決で有効なIP一覧を取得できなかった。"""


async def _resolve_host(host: str) -> list[str]:
    """ホスト名を DNS 解決し、IP 文字列のリストを返す。

    テスト時は本関数を patch することで DNS をモックできる。
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


async def resolve_public_host_addresses(host: str) -> tuple[PublicIpAddress, ...]:
    """ホスト名をDNS解決し、共通のIP方針で検証したアドレス一覧を返す。

    docker compose のサービス名 (``backend``, ``db``, ...) や、A レコードが
    プライベート IP に向いている悪意あるドメインを実フェッチ前に弾く。

    Returns:
        DNS解決結果の順序を保持した、1件以上の検証済みアドレスのタプル。

    Raises:
        HostBlockedError: いずれかの解決結果が public でない。
        HostResolutionError: DNS解決に失敗したか、結果が空またはIP形式でない。

    Note:
        本関数は名前解決結果を検証し、実際の接続先は保証しない。
        直接接続では ``external`` が返却IPへ固定し、プロキシ経由では
        プロキシ自身が解決した接続先に非公開IP拒否を適用する。
    """
    try:
        resolved = await _resolve_host(host)
    except socket.gaierror as e:
        msg = f"DNS resolution failed for host: {host}: {e}"
        raise HostResolutionError(msg) from e

    if not resolved:
        msg = f"DNS resolution returned no addresses for host: {host}"
        raise HostResolutionError(msg)

    addrs: list[PublicIpAddress] = []
    for addr in resolved:
        try:
            addrs.append(PublicIpAddress(addr))
        except NotAPublicIpError as e:
            msg = f"host resolves to non-public address: {host} -> {addr}"
            raise HostBlockedError(msg) from e
        except NotAnIpAddressError as e:
            msg = f"DNS resolution returned an invalid address for host: {host}"
            raise HostResolutionError(msg) from e
    return tuple(addrs)
