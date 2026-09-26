"""取得先へのGETで、非成功応答と通信失敗を共通HTTPエラーとして伝える。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import httpx

from app.http.destination_resolution import HostResolutionError
from app.http.error_mapping import (
    http_response_error_from_exception,
    http_transport_error_from_exception,
)


async def get_source_response(
    client: httpx.AsyncClient,  # noqa: TID251
    url: str,
    *,
    params: Mapping[str, str | int] | None = None,
) -> httpx.Response:
    """成功応答を返し、宛先拒否と通信失敗と確認できない例外は元のまま伝える。"""
    try:
        response = await client.get(url, params=params)
    except (httpx.HTTPError, HostResolutionError) as exc:
        mapped = http_transport_error_from_exception(exc)
        if mapped is None:
            raise
        raise mapped from exc
    received_at = datetime.now(UTC)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise http_response_error_from_exception(exc, received_at=received_at) from exc
    return response
