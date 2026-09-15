"""取得依頼の送信失敗と試行回数から再送を判断する。"""

from app.collection.article_acquisition.acquisition_send_error import (
    AcquisitionSendFailure,
    RetryDisposition,
)

MAX_ACQUISITION_SEND_ATTEMPTS = 3


def should_retry_acquisition_send(
    failure: AcquisitionSendFailure, *, attempt: int
) -> bool:
    return (
        failure.disposition is RetryDisposition.RETRYABLE
        and attempt < MAX_ACQUISITION_SEND_ATTEMPTS
    )
