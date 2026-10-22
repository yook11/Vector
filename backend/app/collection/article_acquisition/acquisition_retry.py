"""取得依頼の送信の失敗が送り直しで解消しうるかと試行回数から、送り直すかを決める。"""

from app.aws.sqs.errors import MessageSendError
from app.aws.sqs.retry_prospect import is_resolvable_by_retry

MAX_ACQUISITION_SEND_ATTEMPTS = 3


def should_retry_acquisition_send(error: MessageSendError, *, attempt: int) -> bool:
    return is_resolvable_by_retry(error) and attempt < MAX_ACQUISITION_SEND_ATTEMPTS
