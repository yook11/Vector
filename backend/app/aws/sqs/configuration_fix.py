"""SQSへの送信の失敗が送信側の設定を直すまで解消しないかを、起きたことの種類から返す。"""

from __future__ import annotations

from app.aws.sqs.errors import (
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSendError,
    SqsServiceError,
    SqsServiceErrorReason,
)


def requires_configuration_fix(error: MessageSendError) -> bool:
    """資格情報・region・権限・送信先のキューを直すまで解消しない送信の失敗かを返す。"""
    match error:
        case MessageSendConfigurationError():
            # キューのURLが空なのは、設定層が起動時に弾いた後の呼び出し側の誤り。
            return error.reason in (
                MessageSendConfigurationReason.MISSING_CREDENTIALS,
                MessageSendConfigurationReason.INCOMPLETE_CREDENTIALS,
                MessageSendConfigurationReason.MISSING_REGION,
            )
        case SqsServiceError():
            return error.reason in (
                SqsServiceErrorReason.AUTHENTICATION_FAILED,
                SqsServiceErrorReason.ACCESS_DENIED,
                SqsServiceErrorReason.DESTINATION_NOT_FOUND,
            )
        # 資格情報の取得の失敗は、一時的か恒久的かを型から区別できない。
        case _:
            return False
