"""SQSへの送信の失敗が送信側の設定を直すまで解消しないかの分類。SQSへ送らず、変換後の失敗を直接渡す。"""

from __future__ import annotations

import pytest

from app.aws.sqs.errors import MessageSendCredentialsError, MessageSendUnexpectedError
from app.aws.sqs.outgoing_message import MessageTooLargeError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.outbox.relay.failure_recording import _requires_configuration_fix
from app.outbox.relay.sender import (
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSendError,
    MessageSendTransportError,
    MessageServiceError,
    MessageServiceErrorReason,
)

C = MessageSendConfigurationReason
Q = MessageServiceErrorReason


@pytest.mark.parametrize(
    "error",
    [
        MessageSendConfigurationError(reason=C.MISSING_CREDENTIALS),
        MessageSendConfigurationError(reason=C.INCOMPLETE_CREDENTIALS),
        MessageSendConfigurationError(reason=C.MISSING_REGION),
        MessageServiceError(
            reason=Q.AUTHENTICATION_FAILED, service_code="InvalidClientTokenId"
        ),
        MessageServiceError(reason=Q.ACCESS_DENIED, service_code="AccessDenied"),
        MessageServiceError(
            reason=Q.DESTINATION_NOT_FOUND, service_code="QueueDoesNotExist"
        ),
    ],
)
def test_missing_credentials_or_region_and_access_rejections_require_fix(
    error: MessageSendError,
) -> None:
    """資格情報・regionがない失敗と、SQSが認証・権限・キュー不在で拒否した失敗は、設定を直すまで解消しない失敗として分類する。"""
    assert _requires_configuration_fix(error) is True


@pytest.mark.parametrize(
    "error",
    [
        MessageSendConfigurationError(reason=C.MISSING_QUEUE_URL),
        MessageSendCredentialsError(exception_type="CredentialRetrievalError"),
        MessageServiceError(reason=Q.THROTTLED, service_code="RequestThrottled"),
        MessageServiceError(reason=Q.UNKNOWN, service_code="FutureError"),
        MessageSendTransportError(
            failure=HttpTransportFailure(
                HttpTransportStage.CONNECT, HttpTransportFailureReason.TIMEOUT
            ),
            exception_type="ConnectTimeoutError",
        ),
        MessageTooLargeError(),
        MessageSendUnexpectedError(exception_type="ParamValidationError"),
    ],
)
def test_other_send_failures_do_not_require_fix(error: MessageSendError) -> None:
    """キューのURLが空の呼び出し、資格情報の取得の失敗、混雑や未分類のSQSの拒否、通信の失敗、本文の大きさ、想定外は、設定の修復が要る失敗として分類しない。"""
    assert _requires_configuration_fix(error) is False
