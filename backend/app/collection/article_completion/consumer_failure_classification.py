"""新経路の失敗から、補完工程の再試行・終了と待機時刻を決める。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
from app.collection.external_fetch_failure import (
    NonRetryableFetchFailure,
    RetryableFetchFailure,
    classify_external_fetch_failure,
)
from app.collection.retry_at import RetryAt


@dataclass(frozen=True, slots=True)
class RetryArticleCompletion:
    """補完を再試行する判断と、追加で待機が必要な時刻を表す。"""

    code: str
    retry_at: RetryAt | None = None
    """再試行可能なUTC日時で、指定なし・無効・0秒・期限経過済みならNone。"""
    requires_investigation: bool = False


@dataclass(frozen=True, slots=True)
class CloseArticleCompletion:
    """補完を終了する判断と、その理由の識別情報を表す。"""

    code: str
    requires_investigation: bool = False


def classify_completion_failure(
    exc: Exception, *, now: datetime
) -> RetryArticleCompletion | CloseArticleCompletion:
    """発生事実を変更せず、補完工程として必要な対処を返す。"""
    match classify_external_fetch_failure(exc, now=now):
        case RetryableFetchFailure(
            code=code, retry_at=retry_at, requires_investigation=investigate
        ):
            return RetryArticleCompletion(
                code=code, retry_at=retry_at, requires_investigation=investigate
            )
        case NonRetryableFetchFailure(code=code):
            return CloseArticleCompletion(code=code)
        case None:
            pass

    if isinstance(exc, ArticleCompletionRejectedError):
        investigate = (
            not exc.defects
            or bool(exc.unmapped)
            or AnalyzableArticleDefect.UNMAPPED_VALIDATION_ERROR in exc.defects
        )
        if investigate:
            return RetryArticleCompletion(code=exc.CODE, requires_investigation=True)
        return CloseArticleCompletion(code=exc.CODE)

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
