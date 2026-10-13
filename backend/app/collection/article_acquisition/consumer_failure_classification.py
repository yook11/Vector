"""取得の失敗から、取得依頼を再配信するかどうかと調査の要否を決める。"""

from __future__ import annotations

from datetime import datetime

from app.collection.article_acquisition.consumer_result import (
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.collection.article_acquisition.errors import (
    ResponseSizeLimitExceededError,
    RssFeedErrors,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.external_fetch_failure import (
    NonRetryableFetchFailure,
    RetryableFetchFailure,
    classify_external_fetch_failure,
)


def classify_acquisition_failure(
    exc: Exception, *, now: datetime
) -> RetryAcquisition | NoRetryAcquisition:
    """再試行で変わりうる失敗と、DB障害・想定外の失敗だけを再配信する。"""
    # 再試行で変わらない失敗は取得元全体が届かなくなるので、人が直す対象とする。
    if isinstance(exc, RssFeedErrors):
        feeds = [
            classify_acquisition_failure(failure.error, now=now)
            for failure in exc.failures
        ]
        investigate = any(feed.requires_investigation for feed in feeds)
        if any(isinstance(feed, RetryAcquisition) for feed in feeds):
            return RetryAcquisition(
                exc, code=exc.CODE, requires_investigation=investigate
            )
        return NoRetryAcquisition(
            exc, code=exc.CODE, requires_investigation=investigate
        )
    if isinstance(exc, UnreadableResponseError | ResponseSizeLimitExceededError):
        return NoRetryAcquisition(exc, code=exc.CODE, requires_investigation=True)
    match classify_external_fetch_failure(exc, now=now):
        case RetryableFetchFailure(code=code, requires_investigation=investigate):
            return RetryAcquisition(exc, code=code, requires_investigation=investigate)
        case NonRetryableFetchFailure(code=code):
            return NoRetryAcquisition(exc, code=code, requires_investigation=True)
        case None:
            return RetryAcquisition(exc, code="unknown", requires_investigation=True)
