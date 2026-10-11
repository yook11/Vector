"""取得の失敗から、取得依頼を再配信するかどうかを決める。"""

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
    if isinstance(exc, RssFeedErrors):
        if any(
            isinstance(
                classify_acquisition_failure(failure.error, now=now), RetryAcquisition
            )
            for failure in exc.failures
        ):
            return RetryAcquisition(exc)
        return NoRetryAcquisition(exc)
    if isinstance(exc, UnreadableResponseError | ResponseSizeLimitExceededError):
        return NoRetryAcquisition(exc)
    match classify_external_fetch_failure(exc, now=now):
        case RetryableFetchFailure():
            return RetryAcquisition(exc)
        case NonRetryableFetchFailure():
            return NoRetryAcquisition(exc)
        case None:
            return RetryAcquisition(exc)
