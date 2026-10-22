"""SQSへの送信の失敗が送り直しで解消しうるかの分類。SQSへ送らず、変換後の失敗を直接渡す。"""

from __future__ import annotations

import pytest

from app.aws.sqs.errors import (
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSendCredentialsError,
    MessageSendError,
    MessageSendTransportError,
    MessageSendUnexpectedError,
    SqsServiceError,
    SqsServiceErrorReason,
)
from app.aws.sqs.outgoing_message import (
    MessageBodyMismatchError,
    MessageTooLargeError,
    SqsResponseField,
    SqsResponseInvalidError,
    SqsResponseInvalidReason,
)
from app.aws.sqs.retry_prospect import is_resolvable_by_retry
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

S = HttpTransportStage
R = HttpTransportFailureReason
Q = SqsServiceErrorReason


@pytest.mark.parametrize(
    "error",
    [
        SqsServiceError(reason=Q.THROTTLED, service_code="RequestThrottled"),
        SqsServiceError(
            reason=Q.SERVICE_UNAVAILABLE, service_code="ServiceUnavailable"
        ),
        MessageSendTransportError(
            failure=HttpTransportFailure(S.CONNECT, R.TIMEOUT),
            exception_type="ConnectTimeoutError",
        ),
        MessageSendTransportError(
            failure=HttpTransportFailure(S.UNKNOWN, R.TLS), exception_type="SSLError"
        ),
    ],
)
def test_sqs_congestion_outage_and_interrupted_transport_are_resolvable(
    error: MessageSendError,
) -> None:
    """SQSの混雑・障害と、送り直せば直りうる通信の失敗(待ち切れ・証明書以外のTLS)は、送り直しで解消しうる失敗として分類する。"""
    assert is_resolvable_by_retry(error) is True


@pytest.mark.parametrize(
    "error",
    [
        SqsServiceError(
            reason=Q.AUTHENTICATION_FAILED, service_code="InvalidClientTokenId"
        ),
        SqsServiceError(reason=Q.ACCESS_DENIED, service_code="AccessDenied"),
        SqsServiceError(
            reason=Q.DESTINATION_NOT_FOUND, service_code="QueueDoesNotExist"
        ),
        SqsServiceError(
            reason=Q.REQUEST_REJECTED, service_code="InvalidParameterValue"
        ),
        SqsServiceError(reason=Q.SECURITY_REJECTED, service_code="IncompleteSignature"),
        SqsServiceError(reason=Q.REQUEST_EXPIRED, service_code="RequestExpired"),
        SqsServiceError(reason=Q.ENCRYPTION_ERROR, service_code="KmsDisabled"),
        SqsServiceError(reason=Q.UNKNOWN, service_code="FutureError"),
        MessageSendTransportError(
            failure=HttpTransportFailure(S.CONNECT, R.CERTIFICATE_VERIFICATION),
            exception_type="SSLError",
        ),
        MessageSendTransportError(
            failure=HttpTransportFailure(S.UNKNOWN, R.UNKNOWN),
            exception_type="HTTPClientError",
        ),
        MessageSendConfigurationError(
            reason=MessageSendConfigurationReason.MISSING_CREDENTIALS
        ),
        MessageSendCredentialsError(exception_type="CredentialRetrievalError"),
        MessageSendUnexpectedError(exception_type="ParamValidationError"),
        MessageTooLargeError(),
        SqsResponseInvalidError(
            reason=SqsResponseInvalidReason.MISSING, field=SqsResponseField.MESSAGE_ID
        ),
        MessageBodyMismatchError(),
    ],
)
def test_other_send_failures_are_not_resolvable(error: MessageSendError) -> None:
    """混雑・障害以外のSQSの拒否、直らない通信の失敗、設定・資格情報の失敗、想定外、本文や応答の問題は、送り直しても解消しない失敗として分類する。"""
    assert is_resolvable_by_retry(error) is False
