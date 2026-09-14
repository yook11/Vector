"""新経路の失敗から、補完工程の再試行・終了と待機時刻を決める。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from app.collection.article_completion.errors import (
    ArticleCompletionRejectedError,
    ArticleContentQualityError,
    ArticleContentTypeError,
    ArticleExtractionCrashedError,
    ArticleExtractionEmptyError,
    FetchDeadlineExceededError,
    ResponseSizeLimitExceededError,
    RobotsDisallowedError,
)
from app.collection.domain.analyzable_article import AnalyzableArticleDefect
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import HttpTransportFailureReason, HttpTransportStage


@dataclass(frozen=True, slots=True)
class RetryArticleCompletion:
    """補完を再試行する判断と、追加で待機が必要な時刻を表す。"""

    code: str
    retry_at: datetime | None = None
    """再試行可能なUTC日時で、指定なし・無効・0秒・期限経過済みならNone。"""
    requires_investigation: bool = False


@dataclass(frozen=True, slots=True)
class CloseArticleCompletion:
    """補完を終了する判断と、その理由の識別情報を表す。"""

    code: str
    requires_investigation: bool = False


def _retry_at(exc: HttpResponseError, *, now: datetime) -> datetime | None:
    """有効な未来の待機時刻をUTCで返し、指定なし・無効・0秒・期限経過済みならNoneを返す。"""
    value = exc.retry_after
    if value is None or not (value := value.strip()):
        return None
    try:
        if value.isascii() and value.isdecimal():
            seconds = int(value)
            if seconds == 0:
                return None
            candidate = exc.received_at.astimezone(UTC) + timedelta(seconds=seconds)
        else:
            candidate = parsedate_to_datetime(value)
            # HTTPの旧asctime形式にはタイムゾーン指定がない。
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=UTC)
            candidate = candidate.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None
    return candidate if candidate > now else None


def classify_completion_failure(
    exc: Exception, *, now: datetime
) -> RetryArticleCompletion | CloseArticleCompletion:
    """発生事実を変更せず、補完工程として必要な対処を返す。"""
    if isinstance(exc, HttpResponseError):
        status = exc.status_code
        investigate = status in (407, 425, 511) or not 300 <= status < 600
        retry = (
            investigate
            or status in (408, 421, 429)
            or (500 <= status < 600 and status not in (501, 505))
        )
        if retry:
            return RetryArticleCompletion(
                code=exc.CODE,
                retry_at=_retry_at(exc, now=now),
                requires_investigation=investigate,
            )
        return CloseArticleCompletion(code=exc.CODE)

    if isinstance(exc, HttpTransportError):
        failure = exc.failure
        return RetryArticleCompletion(
            code=exc.CODE,
            requires_investigation=(
                failure.stage is HttpTransportStage.UNKNOWN
                or failure.reason is HttpTransportFailureReason.UNKNOWN
                or failure.proxy_status in (407, 511)
            ),
        )

    if isinstance(exc, ArticleCompletionRejectedError):
        investigate = (
            not exc.defects
            or bool(exc.unmapped)
            or AnalyzableArticleDefect.UNMAPPED_VALIDATION_ERROR in exc.defects
        )
        if investigate:
            return RetryArticleCompletion(code=exc.CODE, requires_investigation=True)
        return CloseArticleCompletion(code=exc.CODE)

    if isinstance(exc, HostBlockedError):
        return CloseArticleCompletion(code="host_blocked")

    if isinstance(
        exc,
        (
            RobotsDisallowedError,
            ResponseSizeLimitExceededError,
            ArticleContentTypeError,
            ArticleExtractionEmptyError,
            ArticleContentQualityError,
        ),
    ):
        return CloseArticleCompletion(code=exc.CODE)

    if isinstance(exc, (FetchDeadlineExceededError, ArticleExtractionCrashedError)):
        return RetryArticleCompletion(
            code=exc.CODE,
            requires_investigation=isinstance(exc, ArticleExtractionCrashedError),
        )

    return RetryArticleCompletion(code="unknown", requires_investigation=True)
