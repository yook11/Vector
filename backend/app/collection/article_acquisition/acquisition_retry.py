"""取得依頼の送信失敗と試行回数から再送を判断する。"""

from app.aws.sqs.errors import MessageSendError

MAX_ACQUISITION_SEND_ATTEMPTS = 3


def should_retry_acquisition_send(error: MessageSendError, *, attempt: int) -> bool:
    return error.retryable and attempt < MAX_ACQUISITION_SEND_ATTEMPTS
