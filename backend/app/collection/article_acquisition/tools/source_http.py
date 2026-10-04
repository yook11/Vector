"""取得先へのGETで、本文を上限内で受け取り、非成功応答と通信失敗を共通HTTPエラーとして伝える。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2

from app.collection.article_acquisition.errors import ResponseSizeLimitExceededError
from app.collection.response_size import ResponseSizeBasis
from app.http.destination_resolution import HostResolutionError
from app.http.error_mapping import (
    http_response_error_from_exception,
    http_transport_error_from_exception,
)

# 正常な応答より十分大きく、異常な応答だけを拒む上限。
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class SourceResponse:
    """上限内で受け取り終えた成功応答の本文。"""

    content: bytes
    encoding: str
    """Content-Typeのcharset、無ければUTF-8。"""

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")


async def get_source_response(
    client: httpx2.AsyncClient,  # noqa: TID251
    url: str,
    *,
    params: Mapping[str, str | int] | None = None,
) -> SourceResponse:
    """成功応答の本文を返し、宛先拒否と通信失敗と確認できない例外は元のまま伝える。"""
    try:
        async with client.stream("GET", url, params=params) as response:
            received_at = datetime.now(UTC)
            try:
                response.raise_for_status()
            except httpx2.HTTPStatusError as exc:
                raise http_response_error_from_exception(
                    exc, received_at=received_at
                ) from exc
            return SourceResponse(
                content=await _read_body(response),
                encoding=response.encoding or "utf-8",
            )
    except (httpx2.HTTPError, HostResolutionError) as exc:
        mapped = http_transport_error_from_exception(exc)
        if mapped is None:
            raise
        raise mapped from exc


async def _read_body(response: httpx2.Response) -> bytes:
    """上限を超えたチャンクを保持せず、展開後の本文量を制限する。"""
    content_length = response.headers.get("content-length")
    if content_length is not None:
        value = content_length.strip()
        if value.isascii() and value.isdecimal():
            try:
                declared_bytes = int(value)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > _MAX_RESPONSE_BYTES:
                raise ResponseSizeLimitExceededError(
                    limit_bytes=_MAX_RESPONSE_BYTES,
                    observed_bytes=declared_bytes,
                    size_basis=ResponseSizeBasis.DECLARED_CONTENT_LENGTH,
                )

    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_CHUNK_BYTES):
        observed_bytes = len(body) + len(chunk)
        if observed_bytes > _MAX_RESPONSE_BYTES:
            raise ResponseSizeLimitExceededError(
                limit_bytes=_MAX_RESPONSE_BYTES,
                observed_bytes=observed_bytes,
                size_basis=ResponseSizeBasis.RECEIVED_DECODED_BODY,
            )
        body.extend(chunk)
    return bytes(body)
