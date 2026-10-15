"""取得依頼の送信失敗が一時的なものかを事実から判断し、試行回数と合わせて再送を決める。"""

from app.aws.sqs.errors import (
    MessageSendError,
    MessageSendTransportError,
    SqsServiceError,
    SqsServiceErrorReason,
)
from app.http.failure import HttpTransportFailureReason

MAX_ACQUISITION_SEND_ATTEMPTS = 3

_TRANSIENT_TRANSPORT_REASONS = frozenset(
    {
        HttpTransportFailureReason.TIMEOUT,
        HttpTransportFailureReason.DNS_RESOLUTION,
        HttpTransportFailureReason.NETWORK_IO,
        HttpTransportFailureReason.PROTOCOL_VIOLATION,
        HttpTransportFailureReason.UNKNOWN,
    }
)


def is_transient_send_failure(error: MessageSendError) -> bool:
    if isinstance(error, SqsServiceError):
        return error.reason in (
            SqsServiceErrorReason.THROTTLED,
            SqsServiceErrorReason.SERVICE_UNAVAILABLE,
        )
    if isinstance(error, MessageSendTransportError):
        if error.failure.reason is HttpTransportFailureReason.PROXY:
            status = error.failure.proxy_status
            return status is None or status == 429 or 500 <= status <= 599
        return error.failure.reason in _TRANSIENT_TRANSPORT_REASONS
    return False


def should_retry_acquisition_send(error: MessageSendError, *, attempt: int) -> bool:
    return is_transient_send_failure(error) and attempt < MAX_ACQUISITION_SEND_ATTEMPTS
