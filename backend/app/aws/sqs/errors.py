"""SQSへの送信でSDKの呼び出しが失敗したことを、起きたことごとの例外で伝える。"""

import re
from enum import StrEnum

from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)

from app.http.failure import HttpTransportFailure, classify_botocore
from app.shared.errors import ApplicationError


class SqsServiceErrorReason(StrEnum):
    """SQSが返したエラーコードを、何が起きたかの意味で分けた理由。"""

    THROTTLED = "throttled"
    SERVICE_UNAVAILABLE = "service_unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCESS_DENIED = "access_denied"
    DESTINATION_NOT_FOUND = "destination_not_found"
    REQUEST_REJECTED = "request_rejected"
    SECURITY_REJECTED = "security_rejected"
    REQUEST_EXPIRED = "request_expired"
    ENCRYPTION_ERROR = "encryption_error"
    UNKNOWN = "unknown"


class MessageSendConfigurationReason(StrEnum):
    """送信に必要な設定のうち、何がなかったか。"""

    MISSING_CREDENTIALS = "missing_credentials"
    INCOMPLETE_CREDENTIALS = "incomplete_credentials"
    MISSING_REGION = "missing_region"
    MISSING_QUEUE_URL = "missing_queue_url"


class MessageSendError(ApplicationError):
    """送れなかった失敗の基底で、再送するかは受け取った工程が事実から決める。"""


class MessageSendTransportError(MessageSendError):
    """通信が完了しなかった失敗で、SQSが受け付けたかは断定しない。"""

    def __init__(self, *, failure: HttpTransportFailure, exception_type: str) -> None:
        self.failure = failure
        self.exception_type = exception_type
        super().__init__(
            "メッセージの送信で通信が完了しませんでした",
            details={
                "stage": self.failure.stage.value,
                "reason": self.failure.reason.value,
                "proxy_status": self.failure.proxy_status,
                "exception_type": self.exception_type,
            },
        )


class SqsServiceError(MessageSendError):
    """SQSがエラーコードを返して送信を終えた。"""

    def __init__(
        self, *, reason: SqsServiceErrorReason, service_code: str | None
    ) -> None:
        self.reason = reason
        self.service_code = service_code
        super().__init__(
            "SQSがエラーを返し、メッセージを送信できませんでした",
            details={"reason": self.reason.value, "service_code": self.service_code},
        )


class MessageSendConfigurationError(MessageSendError):
    """資格情報・リージョン・送信先のキューなど、送信に必要な設定がない。"""

    def __init__(self, *, reason: MessageSendConfigurationReason) -> None:
        self.reason = reason
        super().__init__(
            "メッセージの送信に必要な設定がありません",
            details={"reason": self.reason.value},
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
    SqsServiceErrorReason.THROTTLED: (
        "RequestThrottled",
        "KmsThrottled",
        "ThrottlingException",
    ),
    SqsServiceErrorReason.SERVICE_UNAVAILABLE: (
        "InternalFailure",
        "ServiceUnavailable",
    ),
    SqsServiceErrorReason.AUTHENTICATION_FAILED: (
        "InvalidClientTokenId",
        "MissingAuthenticationToken",
    ),
    SqsServiceErrorReason.ACCESS_DENIED: (
        "AccessDenied",
        "AccessDeniedException",
        "NotAuthorized",
        "KmsAccessDenied",
        "OptInRequired",
    ),
    SqsServiceErrorReason.DESTINATION_NOT_FOUND: ("QueueDoesNotExist",),
    SqsServiceErrorReason.REQUEST_REJECTED: (
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
    SqsServiceErrorReason.SECURITY_REJECTED: ("InvalidSecurity", "IncompleteSignature"),
    SqsServiceErrorReason.REQUEST_EXPIRED: ("RequestExpired",),
    SqsServiceErrorReason.ENCRYPTION_ERROR: (
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
) -> SqsServiceError:
    """SQSが返したエラーコードを何が起きたかの理由へ翻訳し、形の不正なコードは持たない。"""
    if code is not None and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", code) is None:
        code = None
    reason = _REASON_BY_CODE.get(code) if code is not None else None
    if reason is None:
        # 既知のコードはstatusより優先し、未知のコードだけ5xxをSQS側の障害とみなす。
        reason = (
            SqsServiceErrorReason.SERVICE_UNAVAILABLE
            if status_code is not None and 500 <= status_code <= 599
            else SqsServiceErrorReason.UNKNOWN
        )
    return SqsServiceError(reason=reason, service_code=code)


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
