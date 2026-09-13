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
    """ホスト名の DNS 解決自体に失敗した。"""


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
        DNS解決結果から得た検証済みアドレスのタプル。

    Raises:
        HostBlockedError: いずれかの解決結果が public でない。
        HostResolutionError: DNS 解決に失敗した。

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
