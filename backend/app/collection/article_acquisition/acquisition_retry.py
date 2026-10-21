"""取得依頼の送信失敗が一時的なものかを事実から判断し、試行回数と合わせて再送を決める。"""

from app.aws.sqs.errors import (
    MessageSendError,
    MessageSendTransportError,
    SqsServiceError,
    SqsServiceErrorReason,
)
from app.http.retry_prospect import is_resolvable_by_retry

MAX_ACQUISITION_SEND_ATTEMPTS = 3


def is_transient_send_failure(error: MessageSendError) -> bool:
    if isinstance(error, SqsServiceError):
        return error.reason in (
            SqsServiceErrorReason.THROTTLED,
            SqsServiceErrorReason.SERVICE_UNAVAILABLE,
        )
    if isinstance(error, MessageSendTransportError):
        return is_resolvable_by_retry(error.failure)
    return False


def should_retry_acquisition_send(error: MessageSendError, *, attempt: int) -> bool:
    return is_transient_send_failure(error) and attempt < MAX_ACQUISITION_SEND_ATTEMPTS
