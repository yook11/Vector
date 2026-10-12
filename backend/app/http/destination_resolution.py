"""ホスト名をDNS解決し、外部通信の共通宛先方針を適用する。"""

from __future__ import annotations

import asyncio
import socket

from app.http.destination_policy import (
    DestinationRejectedError,
    DestinationRejectedReason,
    NonPublicIpAddress,
    PublicIpAddress,
    classify_ip_address,
)
from app.http.errors import HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_DNS_RESOLUTION_FAILURE = HttpTransportFailure(
    HttpTransportStage.PREPARATION, HttpTransportFailureReason.DNS_RESOLUTION
)


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
        DestinationRejectedError: いずれかの解決結果が public でない。
        HttpTransportError: DNS解決に失敗した。
        RuntimeError: 解決結果が空またはIP形式でない。

    Note:
        本関数は名前解決結果を検証し、実際の接続先は保証しない。
        直接接続では ``external`` が返却IPへ固定し、プロキシ経由では
        プロキシ自身が解決した接続先に非公開IP拒否を適用する。
    """
    try:
        dns_resolved_ips = await _resolve_host(host)
    except socket.gaierror as e:
        raise HttpTransportError(failure=_DNS_RESOLUTION_FAILURE) from e

    # getaddrinfoは成功時に1件以上の数値IPを返すため、
    # 空やIPでない結果は通信失敗ではなく前提の崩れとして扱う。
    if not dns_resolved_ips:
        raise RuntimeError("名前解決が成功したのに結果が空でした")

    public_ips: list[PublicIpAddress] = []
    for dns_resolved_ip in dns_resolved_ips:
        match classify_ip_address(dns_resolved_ip):
            case PublicIpAddress() as public:
                public_ips.append(public)
            case NonPublicIpAddress():
                raise DestinationRejectedError(
                    reason=DestinationRejectedReason.RESOLVED_TO_NON_PUBLIC,
                    host=host,
                    address=dns_resolved_ip,
                )
            case None:
                raise RuntimeError("名前解決の結果にIPとして読めない値がありました")
    return tuple(public_ips)
