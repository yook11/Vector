"""scrape concern の失敗 value union と Retry 軸分類。

``ExternalFetchError`` は origin error のまま ``FetchFailed`` に保持する。
content 失敗は ``ContentFailure`` value として返し、``ScrapeDecision`` が
closed / retry の後処理方針を表す。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import ClassVar, Final, assert_never

from app.collection.article_completion.retry_policy import (
    BLIP_POLICY,
    OUTAGE_POLICY,
    RETRY_AFTER_POLICY,
    TIMEOUT_POLICY,
    UNKNOWN_POLICY,
    RetryPolicy,
    effective_delay_minutes,
)
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchContentTypeMismatchError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRateLimitedError,
    FetchRedirectBlockedError,
    FetchRedirectLoopError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchResponseTooLargeError,
    FetchRetryableStatusError,
    FetchRobotsDisallowedError,
    FetchRobotsUnavailableError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedStatusError,
)

_BODY_SAMPLE_MAX = 200
_ERROR_MESSAGE_MAX = 500
_CONTENT_TYPE_MAX = 200


# ---------------------------------------------------------------------------
# content 失敗 variant (取得できたが使える本文でなかった)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotHtml:
    """Content-Type が ``text/html`` を含まない。"""

    content_type: str
    reason: ClassVar[str] = "not_html"

    def __post_init__(self) -> None:
        if len(self.content_type) > _CONTENT_TYPE_MAX:
            object.__setattr__(
                self, "content_type", self.content_type[:_CONTENT_TYPE_MAX]
            )


@dataclass(frozen=True)
class ParserGaveUp:
    """``trafilatura.bare_extraction`` が ``None`` を返した。"""

    reason: ClassVar[str] = "parser_gave_up"


@dataclass(frozen=True)
class ParseCrashed:
    """trafilatura parse が例外または想定外戻り値で失敗した。"""

    error_class: str
    error_message: str
    reason: ClassVar[str] = "parse_crashed"

    def __post_init__(self) -> None:
        if len(self.error_message) > _ERROR_MESSAGE_MAX:
            object.__setattr__(
                self, "error_message", self.error_message[:_ERROR_MESSAGE_MAX]
            )


ParseFailure = NotHtml | ParserGaveUp | ParseCrashed
"""RawResponse を HTML document として解釈できなかった失敗 union。"""


@dataclass(frozen=True)
class ContentQualityTooLow:
    """品質ゲートを満たさなかった本文・タイトルの観測値。"""

    body_length: int
    title_present: bool
    body_sample: str | None
    reason: ClassVar[str] = "content_quality_too_low"

    def __post_init__(self) -> None:
        if self.body_sample is not None and len(self.body_sample) > _BODY_SAMPLE_MAX:
            object.__setattr__(self, "body_sample", self.body_sample[:_BODY_SAMPLE_MAX])


ContentFailure = ParseFailure | ContentQualityTooLow
"""取得できたが使える本文でなかった content 失敗 union。"""


# ---------------------------------------------------------------------------
# transport 失敗 variant (接続できなかった)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchFailed:
    """``ExternalFetchError`` を scrape 境界で値化した transport variant。"""

    error: ExternalFetchError


ScrapeFailure = FetchFailed | ContentFailure
"""scrape 境界の全失敗を表す閉じ union。"""


# ---------------------------------------------------------------------------
# Retry 軸の処理方針 (decision)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Terminal:
    """scrape retry を行わず pending を ``closed`` に閉じる失敗。"""

    reason_code: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Retryable:
    """DB 駆動 retry する失敗。"""

    reason_code: str
    policy: RetryPolicy
    retry_after_seconds: float | None = None
    detail: str | None = None

    def is_exhausted(self, attempt_count: int) -> bool:
        """この試行番号で打ち切りか (``>= policy.max_attempts``)。"""
        return attempt_count >= self.policy.max_attempts

    def next_ready_at(self, *, now: datetime, attempt_count: int) -> datetime:
        """次回 retry の ``ready_at`` を算出する純関数。"""
        delay_minutes = effective_delay_minutes(
            self.policy,
            retry_after_seconds=self.retry_after_seconds,
            attempt_count=attempt_count,
        )
        return now + timedelta(minutes=delay_minutes)

    @property
    def policy_code(self) -> str:
        """log 用の policy 識別子 (handler が ``.policy`` を覗かずに済む)。"""
        return self.policy.code


ScrapeDecision = Terminal | Retryable
"""scrape 失敗の Retry 軸での処理方針。"""


# ---------------------------------------------------------------------------
# ExternalFetchError の分類
# ---------------------------------------------------------------------------

# 再試行しても結果が変わらない origin failure。
_TERMINAL_FETCH_ERROR_TYPES: tuple[type[ExternalFetchError], ...] = (
    FetchAccessDeniedError,
    FetchLegalBlockError,
    FetchResourceNotFoundError,
    FetchSsrfBlockedError,
    FetchRobotsDisallowedError,
    FetchRobotsUnavailableError,
    FetchRedirectBlockedError,
    FetchRedirectLoopError,
    FetchResponseTooLargeError,
    FetchContentTypeMismatchError,
)

# ``FetchOriginServerError`` は instance state (reason / retry_after_seconds) を
# 読むため表に入れず ``classify_external_fetch_error`` 内で明示分岐する。
_RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY: Final[
    Mapping[RetryPolicy, tuple[type[ExternalFetchError], ...]]
] = MappingProxyType(
    {
        BLIP_POLICY: (
            FetchGatewayError,
            FetchNetworkError,
        ),
        TIMEOUT_POLICY: (FetchTimeoutError,),
        UNKNOWN_POLICY: (
            FetchRateLimitedError,
            FetchRequestTimeoutError,
            FetchRetryableStatusError,
            FetchUnexpectedStatusError,
        ),
    }
)

# exact type → decision の lookup 表。
_FETCH_DISPOSITION_BY_TYPE: dict[type[ExternalFetchError], ScrapeDecision] = {
    **{t: Terminal(reason_code=t.CODE) for t in _TERMINAL_FETCH_ERROR_TYPES},
    **{
        t: Retryable(reason_code=t.CODE, policy=policy)
        for policy, types in _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY.items()
        for t in types
    },
}


def classify_external_fetch_error(exc: ExternalFetchError) -> ScrapeDecision:
    """origin fetch error を completion scrape 用 decision に分類する。

    ``FetchOriginServerError`` は ``reason`` / ``retry_after_seconds`` を読むため
    明示分岐する。未登録 error は保守的に ``UNKNOWN_POLICY`` retry とする。
    """
    if isinstance(exc, FetchOriginServerError):
        if exc.reason == "service_unavailable" and exc.retry_after_seconds is not None:
            return Retryable(
                reason_code=exc.CODE,
                policy=RETRY_AFTER_POLICY,
                retry_after_seconds=exc.retry_after_seconds,
            )
        return Retryable(reason_code=exc.CODE, policy=OUTAGE_POLICY)

    decision = _FETCH_DISPOSITION_BY_TYPE.get(type(exc))
    if decision is not None:
        return decision
    return Retryable(reason_code=exc.CODE, policy=UNKNOWN_POLICY)


# ---------------------------------------------------------------------------
# ScrapeFailure の分類
# ---------------------------------------------------------------------------


def classify_scrape_failure(failure: ScrapeFailure) -> ScrapeDecision:
    """scrape failure を ``Terminal | Retryable`` に分類する。"""
    if isinstance(failure, FetchFailed):
        err = failure.error
        return replace(
            classify_external_fetch_error(err),
            detail=f"{type(err).__name__}: {err}",
        )

    detail: str | None
    match failure:
        case NotHtml(content_type=ct):
            detail = f"content_type={ct}"
        case ParserGaveUp():
            detail = None
        case ParseCrashed(error_class=ec, error_message=em):
            detail = f"{ec}: {em}"
        case ContentQualityTooLow(body_length=bl, title_present=tp, body_sample=bs):
            sample = f" sample={bs!r}" if bs else ""
            detail = f"body_length={bl} title_present={tp}{sample}"
        case _ as unreachable:
            assert_never(unreachable)
    return Terminal(reason_code=f"scrape_{failure.reason}", detail=detail)
