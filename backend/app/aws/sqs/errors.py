"""SQSへの送信でSDKの呼び出しが失敗したことを、起きたことごとの例外で伝える。"""

import re

from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)

from app.http.failure import classify_botocore
from app.outbox.relay.sender import (
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSendError,
    MessageSendTransportError,
    MessageServiceError,
    MessageServiceErrorReason,
)


class MessageSendCredentialsError(MessageSendError):
    """資格情報の取得先から、送信に使う資格情報を取得できなかった。"""

    def __init__(self, *, exception_type: str) -> None:
        self.exception_type = exception_type
        super().__init__(
            "メッセージの送信に使う資格情報を取得できませんでした",
            details={"exception_type": self.exception_type},
        )


class MessageSendUnexpectedError(MessageSendError):
    """どの分類にも当たらない例外で送れなかった。"""

    def __init__(self, *, exception_type: str) -> None:
        self.exception_type = exception_type
        super().__init__(
            "想定外の理由でメッセージを送信できませんでした",
            details={"exception_type": self.exception_type},
        )


_CODES_BY_REASON = {
    MessageServiceErrorReason.THROTTLED: (
        "RequestThrottled",
        "KmsThrottled",
        "ThrottlingException",
    ),
    MessageServiceErrorReason.SERVICE_UNAVAILABLE: (
        "InternalFailure",
        "ServiceUnavailable",
    ),
    MessageServiceErrorReason.AUTHENTICATION_FAILED: (
        "InvalidClientTokenId",
        "MissingAuthenticationToken",
    ),
    MessageServiceErrorReason.ACCESS_DENIED: (
        "AccessDenied",
        "AccessDeniedException",
        "NotAuthorized",
        "KmsAccessDenied",
        "OptInRequired",
    ),
    MessageServiceErrorReason.DESTINATION_NOT_FOUND: ("QueueDoesNotExist",),
    MessageServiceErrorReason.REQUEST_REJECTED: (
        "InvalidAddress",
        "InvalidMessageContents",
        "UnsupportedOperation",
        "InvalidAction",
        "InvalidParameterCombination",
        "InvalidParameterValue",
        "InvalidQueryParameter",
        "MalformedQueryString",
        "MissingAction",
        "MissingParameter",
        "ValidationError",
        "BatchEntryIdsNotDistinct",
        "BatchRequestTooLong",
        "EmptyBatchRequest",
        "InvalidBatchEntryId",
        "TooManyEntriesInBatchRequest",
    ),
    MessageServiceErrorReason.SECURITY_REJECTED: (
        "InvalidSecurity",
        "IncompleteSignature",
    ),
    MessageServiceErrorReason.REQUEST_EXPIRED: ("RequestExpired",),
    MessageServiceErrorReason.ENCRYPTION_ERROR: (
        "KmsDisabled",
        "KmsInvalidState",
        "KmsNotFound",
        "KmsOptInRequired",
        "KmsInvalidKeyUsage",
    ),
}
_REASON_BY_CODE = {
    code: reason for reason, codes in _CODES_BY_REASON.items() for code in codes
}


def to_sqs_service_error(
    code: str | None, *, status_code: int | None = None
) -> MessageServiceError:
    """SQSが返したエラーコードを何が起きたかの理由へ翻訳し、形の不正なコードは持たない。"""
    if code is not None and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", code) is None:
        code = None
    reason = _REASON_BY_CODE.get(code) if code is not None else None
    if reason is None:
        # 既知のコードはstatusより優先し、未知のコードだけ5xxをSQS側の障害とみなす。
        reason = (
            MessageServiceErrorReason.SERVICE_UNAVAILABLE
            if status_code is not None and 500 <= status_code <= 599
            else MessageServiceErrorReason.UNKNOWN
        )
    return MessageServiceError(reason=reason, service_code=code)


def to_message_send_error(exc: Exception) -> MessageSendError:
    """自由文を保持せず、SDK例外を起きたことごとの送信失敗へ変換する。"""
    if isinstance(exc, MessageSendError):
        return exc
    exception_type = type(exc).__name__
    if isinstance(exc, NoCredentialsError):
        return MessageSendConfigurationError(
            reason=MessageSendConfigurationReason.MISSING_CREDENTIALS
        )
    if isinstance(exc, PartialCredentialsError):
        return MessageSendConfigurationError(
            reason=MessageSendConfigurationReason.INCOMPLETE_CREDENTIALS
        )
    if isinstance(exc, NoRegionError):
        return MessageSendConfigurationError(
            reason=MessageSendConfigurationReason.MISSING_REGION
        )
    if isinstance(exc, CredentialRetrievalError):
        return MessageSendCredentialsError(exception_type=exception_type)
    if isinstance(exc, ClientError):
        # SQSへの送信の中でSQS以外の操作を呼ぶのは、資格情報のproviderだけ。
        if exc.operation_name not in ("SendMessage", "SendMessageBatch"):
            return MessageSendCredentialsError(exception_type=exception_type)
        response = exc.response if isinstance(exc.response, dict) else {}
        error = response.get("Error")
        error = error if isinstance(error, dict) else {}
        # SQSが互換用の別名のコードを返すと、SDKは元のコードをQueryErrorCodeへ移す。
        query_code = error.get("QueryErrorCode")
        code = (
            query_code
            if isinstance(query_code, str) and query_code
            else error.get("Code")
        )
        metadata = response.get("ResponseMetadata")
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        return to_sqs_service_error(
            code if isinstance(code, str) else None,
            status_code=status if type(status) is int else None,
        )
    transport = classify_botocore(exc)
    if transport is not None:
        return MessageSendTransportError(
            failure=transport, exception_type=exception_type
        )
    return MessageSendUnexpectedError(exception_type=exception_type)
