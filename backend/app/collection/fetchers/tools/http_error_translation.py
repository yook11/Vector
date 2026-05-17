"""HTTP status / transport 例外 → ``ExternalFetchError`` 写像の SSoT。

Stage 1 共通 tools (rss_parser / raw_http_client /
algolia_hn_client / crossref_client) が個別に ``status_code`` を直書きすると
分類がズレる (旧 article_completion の extractor は 401 を
access-denied 扱いしていなかった)。
本モジュールに写像を 1 箇所集約し、表駆動テストで写像を固定する。

- ``classify_fetch_status`` は **httpx 非依存の純関数** (status + headers のみ)。
  status → origin error の唯一の対応表。表外 status は保守的に
  ``FetchUnexpectedStatusError`` へ倒す。
- ``translate_fetch_exception`` は httpx / SSRF guard 例外を
  ``classify_fetch_status`` または transport 系 origin error に振り分ける薄い
  wrapper。``httpx.TimeoutException`` は ``RequestError`` の subclass なので
  先に判定する。
- ``RECOVERABLE_FETCH_ERRORS`` は将来の marker 分割が共有する「再試行で
  回復しうる」origin error tuple の単一 SSoT。旧 ``TemporaryFetchError``
  発生条件の忠実訳 (分類 pinning テスト + 将来 marker 用に保持)。
  nasa / cornell の per-feed skip は本 tuple ではなく
  ``MultiFeedRssAdapter`` 経由で ``ExternalFetchError`` **全体** を
  catch する (1 feed の失敗種別を問わず次 feed へ継続する設計のため)。
"""

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

RECOVERABLE_FETCH_ERRORS: tuple[type[ExternalFetchError], ...] = (
    FetchTimeoutError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchGatewayError,
    FetchRequestTimeoutError,
    FetchRateLimitedError,
    FetchRetryableStatusError,
    FetchUnexpectedStatusError,
)
"""再試行で回復しうる origin error の tuple SSoT。

旧 ``TemporaryFetchError`` を raise していた条件 (5xx / 502・504 / 408 / 429 /
425 / 未分類 status / transport timeout / network) の忠実訳。bubble 側
(将来の marker 分割で source 全体失敗として伝播させる対象) は AccessDenied /
LegalBlock / ResourceNotFound / Ssrf / Robots* / Redirect* /
ResponseTooLarge / ContentTypeMismatch / Parse。

nasa / cornell の per-feed skip は本 tuple を ``except`` しない。
``MultiFeedRssAdapter`` が ``ExternalFetchError`` 全体を per-feed で
catch し (種別問わず ``source_feed_fetch_failed`` ログ + 次 feed 継続)、
全 feed 失敗時のみ最初の error を再 raise する。
"""


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """``Retry-After`` header (delta-seconds 形式) を float で返す。

    HTTP-date 形式 (RFC 7231) は本 stage では解釈しない。origin metadata として
    残すのは delta-seconds のみで、parse 不能 / 負値は ``None``。
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
    """HTTP status + headers を origin ``ExternalFetchError`` に写像する純関数。

    httpx 非依存。status → error の唯一の対応表 (SSoT)。表外 status は
    ``FetchUnexpectedStatusError`` へ保守的に倒す。
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
    """httpx / SSRF guard 例外を origin ``ExternalFetchError`` に翻訳する。

    ``httpx.HTTPStatusError`` は status / headers を取り出して
    ``classify_fetch_status`` に委譲。transport 系は種別ごとに固有 origin error。
    SSRF guard の ``HostBlockedError`` は policy block、``HostResolutionError``
    は DNS 解決失敗 (= network) に倒す。呼出側の ``except`` が広すぎて未知の例外
    型が来た場合は保守的に ``FetchNetworkError`` (recoverable) へ倒し、失敗経路
    自体が翻訳例外で落ちないようにする。
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
