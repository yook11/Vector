"""HTTP status / transport 例外 → ``ExternalFetchError`` 写像の SSoT。"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRateLimitedError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchRetryableStatusError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedStatusError,
)
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """``Retry-After`` header を delta-seconds の float で返す。

    HTTP-date 形式 (RFC 7231) は解釈しない。parse 不能 / 負値は ``None``。
    """
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def classify_fetch_status(
    status_code: int,
    headers: Mapping[str, str],
) -> ExternalFetchError:
    """HTTP status + headers を ``ExternalFetchError`` に写像する純関数。

    表外 status は ``FetchUnexpectedStatusError`` へ倒す。
    """
    if status_code in (401, 403):
        return FetchAccessDeniedError(
            status_code=status_code,
            reason="unauthorized" if status_code == 401 else "forbidden",
        )
    if status_code == 451:
        return FetchLegalBlockError(status_code=status_code)
    if status_code in (404, 410):
        return FetchResourceNotFoundError(
            status_code=status_code,
            reason="not_found" if status_code == 404 else "gone",
        )
    if status_code == 429:
        return FetchRateLimitedError(
            status_code=status_code,
            retry_after_seconds=_retry_after_seconds(headers),
        )
    if status_code in (500, 503):
        return FetchOriginServerError(
            status_code=status_code,
            reason=("internal_error" if status_code == 500 else "service_unavailable"),
            retry_after_seconds=(
                _retry_after_seconds(headers) if status_code == 503 else None
            ),
        )
    if status_code in (502, 504):
        return FetchGatewayError(status_code=status_code)
    if status_code == 408:
        return FetchRequestTimeoutError(status_code=status_code)
    if status_code == 425:
        return FetchRetryableStatusError(status_code=status_code, reason="too_early")
    return FetchUnexpectedStatusError(status_code=status_code)


def translate_fetch_exception(
    exc: Exception,
    *,
    source_name: str,
) -> ExternalFetchError:
    """httpx / SSRF guard 例外を ``ExternalFetchError`` に翻訳する。

    未知の例外型は保守的に ``FetchNetworkError`` へ倒す。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_fetch_status(
            exc.response.status_code,
            exc.response.headers,
        )
    # TimeoutException は RequestError の subclass なので先に判定する。
    if isinstance(exc, httpx.TimeoutException):
        return FetchTimeoutError(f"timeout: {source_name}: {exc}")
    if isinstance(exc, httpx.RequestError):
        return FetchNetworkError(f"request error: {source_name}: {exc}")
    if isinstance(exc, HostBlockedError):
        return FetchSsrfBlockedError(str(exc))
    if isinstance(exc, HostResolutionError):
        return FetchNetworkError(str(exc))
    return FetchNetworkError(f"unexpected fetch error: {source_name}: {exc}")
