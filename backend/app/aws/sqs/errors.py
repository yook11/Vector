"""SQSへの送信でSDKの呼び出しが失敗したことを、起きたことごとの例外で伝える。"""

import re
from enum import StrEnum

from botocore.exceptions import (
    ClientError,
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


class MessageSendError(ApplicationError):
    """送れなかった失敗の基底で、再送するかは受け取った工程が事実から決める。"""


class MessageSendTransportError(MessageSendError):
    """通信が完了しなかった失敗で、SQSが受け付けたかは断定しない。"""

    def __init__(self, *, failure: HttpTransportFailure, exception_type: str) -> None:
        super().__init__(
            "メッセージの送信で通信が完了しませんでした",
            details={
                "stage": failure.stage.value,
                "reason": failure.reason.value,
                "proxy_status": failure.proxy_status,
                "exception_type": exception_type,
            },
        )
        self.failure = failure


class SqsServiceError(MessageSendError):
    """SQSがエラーコードを返して送信を終えた。"""

    def __init__(
        self, *, reason: SqsServiceErrorReason, service_code: str | None
    ) -> None:
        super().__init__(
            "SQSがエラーを返し、メッセージを送信できませんでした",
            details={"reason": reason.value, "service_code": service_code},
        )
        self.reason = reason
        self.service_code = service_code


class MessageSendConfigurationError(MessageSendError):
    """資格情報・リージョン・送信先のキューなど、送信に必要な設定がない。"""

    def __init__(self, *, exception_type: str | None = None) -> None:
        super().__init__(
            "メッセージの送信に必要な設定がありません",
            details={"exception_type": exception_type} if exception_type else None,
        )


class MessageSendUnexpectedError(MessageSendError):
    """どの分類にも当たらない例外で送れなかった。"""

    def __init__(self, *, exception_type: str) -> None:
        super().__init__(
            "想定外の理由でメッセージを送信できませんでした",
            details={"exception_type": exception_type},
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
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError, NoRegionError)):
        return MessageSendConfigurationError(exception_type=exception_type)
    if isinstance(exc, ClientError) and exc.operation_name == "SendMessage":
        response = exc.response if isinstance(exc.response, dict) else {}
        error = response.get("Error")
        code = error.get("Code") if isinstance(error, dict) else None
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
