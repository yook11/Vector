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
    FetchRedirectBlockedError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchRetryableStatusError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedClientStatusError,
    FetchUnexpectedServerStatusError,
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


def external_fetch_error_from_http_status(
    status_code: int,
    headers: Mapping[str, str],
) -> ExternalFetchError:
    """HTTP status + headers を ``ExternalFetchError`` に写像する純関数。

    明示表に無い status は ``status_code // 100`` で割って倒す: 3xx は redirect
    blocked、5xx は retryable な server status、それ以外 (4xx + 1xx + 範囲外) は
    分類不能 client status として terminal に倒す。
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

    status_class = status_code // 100
    if status_class == 3:
        return FetchRedirectBlockedError(status_code=status_code)  # Location は読まない
    if status_class == 5:
        return FetchUnexpectedServerStatusError(status_code=status_code)
    # 4xx + 1xx + 範囲外 (600/700 等)。分類不能 status を terminal に倒す。
    return FetchUnexpectedClientStatusError(status_code=status_code)


def external_fetch_error_from_exception(
    exc: Exception,
    *,
    target_label: str,
) -> ExternalFetchError:
    """httpx / SSRF guard 例外を ``ExternalFetchError`` に翻訳する。

    未知の例外型は保守的に ``FetchNetworkError`` へ倒す。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return external_fetch_error_from_http_status(
            exc.response.status_code,
            exc.response.headers,
        )
    # TimeoutException は RequestError の subclass なので先に判定する。
    if isinstance(exc, httpx.TimeoutException):
        return FetchTimeoutError(f"timeout: {target_label}: {exc}")
    if isinstance(exc, httpx.RequestError):
        return FetchNetworkError(f"request error: {target_label}: {exc}")
    if isinstance(exc, HostBlockedError):
        return FetchSsrfBlockedError(str(exc))
    if isinstance(exc, HostResolutionError):
        return FetchNetworkError(str(exc))
    return FetchNetworkError(f"unexpected fetch error: {target_label}: {exc}")
