"""新経路の失敗から、補完工程の再試行・終了と待機時刻を決める。"""

from __future__ import annotations

from datetime import datetime

from app.collection.article_completion.consumer_result import (
    NoRetryCompletion,
    RetryCompletion,
)
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


def classify_completion_failure(
    exc: Exception, *, now: datetime
) -> RetryCompletion | NoRetryCompletion:
    """発生事実を変更せず、補完工程として必要な対処を返す。"""
    match classify_external_fetch_failure(exc, now=now):
        case RetryableFetchFailure(
            code=code, retry_at=retry_at, requires_investigation=investigate
        ):
            return RetryCompletion(
                error=exc,
                code=code,
                retry_at=retry_at,
                requires_investigation=investigate,
            )
        case NonRetryableFetchFailure(code=code):
            return NoRetryCompletion(cause=exc, code=code)
        case None:
            pass

    if isinstance(exc, ArticleCompletionRejectedError):
        investigate = (
            not exc.defects
            or bool(exc.unmapped)
            or AnalyzableArticleDefect.UNMAPPED_VALIDATION_ERROR in exc.defects
        )
        if investigate:
            return RetryCompletion(
                error=exc, code=exc.CODE, requires_investigation=True
            )
        return NoRetryCompletion(cause=exc, code=exc.CODE)

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
        return NoRetryCompletion(cause=exc, code=exc.CODE)

    if isinstance(exc, (FetchDeadlineExceededError, ArticleExtractionCrashedError)):
        return RetryCompletion(
            error=exc,
            code=exc.CODE,
            requires_investigation=isinstance(exc, ArticleExtractionCrashedError),
        )

    return RetryCompletion(error=exc, code="unknown", requires_investigation=True)
