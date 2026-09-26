"""取得の失敗から、取得依頼を再配信するかどうかを決める。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.collection.article_acquisition.errors import RssFeedErrors
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.external_fetch_failure import (
    NonRetryableFetchFailure,
    RetryableFetchFailure,
    classify_external_fetch_failure,
)


@dataclass(frozen=True, slots=True)
class RetryAcquisition:
    """SQS の再配信に任せる取得の失敗。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class NoRetryAcquisition:
    """再試行しても変わらないため受信完了にし、次の定期投入に任せる取得の失敗。"""

    error: Exception


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
    if isinstance(exc, UnreadableResponseError):
        return NoRetryAcquisition(exc)
    match classify_external_fetch_failure(exc, now=now):
        case RetryableFetchFailure():
            return RetryAcquisition(exc)
        case NonRetryableFetchFailure():
            return NoRetryAcquisition(exc)
        case None:
            return RetryAcquisition(exc)
