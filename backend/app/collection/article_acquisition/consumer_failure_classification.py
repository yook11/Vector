"""取得の失敗から、取得依頼を再配信するか断念するかを決める。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from app.collection.article_acquisition.errors import RssFeedErrors
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.external_fetch_failure import (
    NonRetryableFetchFailure,
    RetryableFetchFailure,
    classify_external_fetch_failure,
)


class AcquisitionFailureDecision(StrEnum):
    """取得依頼の失敗後の扱いで、value は監査の ``failure_action`` に使う。"""

    RETRY = "retry"
    """SQS の再配信に任せる。"""
    ABANDON = "abandon"
    """再試行しても変わらないため、この依頼を終えて次の定期投入に任せる。"""


def classify_acquisition_failure(
    exc: Exception, *, now: datetime
) -> AcquisitionFailureDecision:
    """再試行で変わりうる失敗と、DB障害・想定外の失敗だけを再配信する。"""
    if isinstance(exc, RssFeedErrors):
        if any(
            classify_acquisition_failure(failure.error, now=now)
            is AcquisitionFailureDecision.RETRY
            for failure in exc.failures
        ):
            return AcquisitionFailureDecision.RETRY
        return AcquisitionFailureDecision.ABANDON
    if isinstance(exc, UnreadableResponseError):
        return AcquisitionFailureDecision.ABANDON
    match classify_external_fetch_failure(exc, now=now):
        case RetryableFetchFailure():
            return AcquisitionFailureDecision.RETRY
        case NonRetryableFetchFailure():
            return AcquisitionFailureDecision.ABANDON
        case None:
            return AcquisitionFailureDecision.RETRY
