"""SQSへの送信の失敗が送り直しで解消しうるかを、起きたことの種類から返す。"""

from __future__ import annotations

from app.aws.sqs.errors import (
    MessageSendError,
    MessageSendTransportError,
    SqsServiceError,
    SqsServiceErrorReason,
)
from app.http import retry_prospect as http_retry_prospect


def is_resolvable_by_retry(error: MessageSendError) -> bool:
    """同じメッセージを送り直すだけで解消しうる送信の失敗かを返す。"""
    match error:
        case MessageSendTransportError():
            return http_retry_prospect.is_resolvable_by_retry(error.failure)
        case SqsServiceError():
            return error.reason in (
                SqsServiceErrorReason.THROTTLED,
                SqsServiceErrorReason.SERVICE_UNAVAILABLE,
            )
        # 設定・資格情報・本文や応答の問題と想定外の失敗は、直すまで変わらない。
        case _:
            return False
