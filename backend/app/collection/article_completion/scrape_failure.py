"""scrape concern の失敗 value union と Retry 軸分類。

``ExternalFetchError`` は origin error のまま ``ScrapeFailure`` に保持する。content
失敗は本文を取得できたが使えなかった観測値で、``classify_scrape_failure`` が両者を
``ScrapeTerminal | ScrapeRetryable`` に写像する。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import ClassVar

from app.collection.article_completion.retry_policy import (
    BLIP,
    OUTAGE,
    TIMEOUT,
    UNKNOWN,
    FixedDelay,
    RetryDelay,
    RetrySchedule,
)
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchGatewayError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRateLimitedError,
    FetchRequestTimeoutError,
    FetchRetryableStatusError,
    FetchTimeoutError,
    FetchUnexpectedStatusError,
)

_BODY_SAMPLE_MAX = 200
_ERROR_MESSAGE_MAX = 500
_CONTENT_TYPE_MAX = 200


# ---------------------------------------------------------------------------
# Retry 軸の処理方針 (decision)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScrapeTerminal:
    """scrape retry を行わず pending を ``closed`` に閉じる失敗。"""

    reason_code: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ScrapeRetryable:
    """DB 駆動 retry する失敗。``next_delay`` は解決済みの単一遅延。"""

    reason_code: str
    max_attempts: int
    next_delay: RetryDelay
    detail: str | None = None

    def is_exhausted(self, attempt_count: int) -> bool:
        """この試行番号で打ち切りか (``>= max_attempts``)。"""
        return attempt_count >= self.max_attempts

    def next_ready_at(self, *, now: datetime, attempt_count: int) -> datetime:
        """次回 retry の ``ready_at`` を算出する純関数。"""
        return now + timedelta(minutes=self.next_delay.minutes(attempt_count))


# ---------------------------------------------------------------------------
# content 失敗 variant (取得できたが使える本文でなかった)
# ---------------------------------------------------------------------------
#
# ``reason`` は失敗種別タグ (純データ)。terminal への写像は
# ``classify_scrape_failure`` が一手に担い、VO 自身は分類を持たない。


@dataclass(frozen=True)
class ScrapeNotHtml:
    """Content-Type が ``text/html`` を含まない。"""

    content_type: str
    reason: ClassVar[str] = "not_html"

    def __post_init__(self) -> None:
        if len(self.content_type) > _CONTENT_TYPE_MAX:
            object.__setattr__(
                self, "content_type", self.content_type[:_CONTENT_TYPE_MAX]
            )


@dataclass(frozen=True)
class ScrapeParserGaveUp:
    """``trafilatura.bare_extraction`` が ``None`` を返した。"""

    reason: ClassVar[str] = "parser_gave_up"


@dataclass(frozen=True)
class ScrapeParseCrashed:
    """trafilatura parse が例外または想定外戻り値で失敗した。"""

    error_class: str
    error_message: str
    reason: ClassVar[str] = "parse_crashed"

    def __post_init__(self) -> None:
        if len(self.error_message) > _ERROR_MESSAGE_MAX:
            object.__setattr__(
                self, "error_message", self.error_message[:_ERROR_MESSAGE_MAX]
            )


@dataclass(frozen=True)
class ScrapeContentQualityTooLow:
    """品質ゲートを満たさなかった本文・タイトルの観測値。"""

    body_length: int
    title_present: bool
    body_sample: str | None
    reason: ClassVar[str] = "content_quality_too_low"

    def __post_init__(self) -> None:
        if self.body_sample is not None and len(self.body_sample) > _BODY_SAMPLE_MAX:
            object.__setattr__(self, "body_sample", self.body_sample[:_BODY_SAMPLE_MAX])


ScrapeContentFailure = (
    ScrapeNotHtml | ScrapeParserGaveUp | ScrapeParseCrashed | ScrapeContentQualityTooLow
)
"""取得できたが使える本文でなかった content 失敗 union。"""


ScrapeFailure = ExternalFetchError | ScrapeContentFailure
"""scrape 境界の全失敗を表す閉じ union (transport は origin error のまま値化)。"""


# ---------------------------------------------------------------------------
# ExternalFetchError の分類
# ---------------------------------------------------------------------------


def _retryable(
    exc: ExternalFetchError,
    schedule: RetrySchedule,
    *,
    override: RetryDelay | None = None,
) -> ScrapeRetryable:
    """origin error を schedule テンプレートから ``ScrapeRetryable`` に組み立てる。

    ``override`` は server 指示 (``Retry-After``) で schedule を差し替える場合に渡す。
    """
    return ScrapeRetryable(
        reason_code=exc.CODE,
        max_attempts=schedule.max_attempts,
        next_delay=override if override is not None else schedule.delay,
    )


def classify_external_fetch_error(
    exc: ExternalFetchError,
) -> ScrapeTerminal | ScrapeRetryable:
    """origin fetch error を completion scrape 用 decision に分類する。

    retry 可否は origin の ``retryable`` (SSoT) に従い、retryable=False は段に依らず
    ``ScrapeTerminal``。retryable=True は単一 ``match`` で backoff schedule に写像する。
    instance state を読むケース (503 / 429 の ``Retry-After``) を先頭に置き、
    server 指示があれば ``FixedDelay`` で schedule を上書きする。``_`` は冒頭で
    terminal を弾いた後に残る未登録 retryable のための保守的 fallback。
    """
    if not exc.retryable:
        return ScrapeTerminal(reason_code=exc.CODE)

    match exc:
        case FetchOriginServerError(
            reason="service_unavailable", retry_after_seconds=float() as ra
        ):
            return _retryable(exc, OUTAGE, override=FixedDelay(ra))
        case FetchOriginServerError():
            return _retryable(exc, OUTAGE)
        case FetchRateLimitedError(retry_after_seconds=float() as ra):
            return _retryable(exc, UNKNOWN, override=FixedDelay(ra))
        case FetchRateLimitedError():
            return _retryable(exc, UNKNOWN)
        case FetchGatewayError() | FetchNetworkError():
            return _retryable(exc, BLIP)
        case FetchTimeoutError():
            return _retryable(exc, TIMEOUT)
        case (
            FetchRequestTimeoutError()
            | FetchRetryableStatusError()
            | FetchUnexpectedStatusError()
        ):
            return _retryable(exc, UNKNOWN)
        case _:
            return _retryable(exc, UNKNOWN)


# ---------------------------------------------------------------------------
# ScrapeFailure の分類
# ---------------------------------------------------------------------------


def classify_scrape_failure(
    failure: ScrapeFailure,
) -> ScrapeTerminal | ScrapeRetryable:
    """scrape failure を ``ScrapeTerminal | ScrapeRetryable`` に分類する。

    transport (origin error) は ``classify_external_fetch_error`` に委譲し、例外の
    class+message を ``detail`` に畳む。content 失敗は常に ``ScrapeTerminal`` に閉じる。
    ``case _`` は新 content variant の写像漏れを早期に露見させる totality guard。
    """
    if isinstance(failure, ExternalFetchError):
        return replace(
            classify_external_fetch_error(failure),
            detail=f"{type(failure).__name__}: {failure}",
        )

    match failure:
        case ScrapeNotHtml():
            detail = f"content_type={failure.content_type}"
        case ScrapeParserGaveUp():
            detail = None
        case ScrapeParseCrashed():
            detail = f"{failure.error_class}: {failure.error_message}"
        case ScrapeContentQualityTooLow():
            sample = f" sample={failure.body_sample!r}" if failure.body_sample else ""
            detail = (
                f"body_length={failure.body_length} "
                f"title_present={failure.title_present}{sample}"
            )
        case _:
            raise AssertionError(f"unmapped scrape content failure: {type(failure)!r}")
    return ScrapeTerminal(reason_code=f"scrape_{failure.reason}", detail=detail)
