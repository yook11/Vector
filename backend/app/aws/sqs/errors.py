"""SQSへメッセージを送れなかった失敗を、起きたことごとの例外で伝える。"""

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
    """SQSが返したエラーコードを、性質ごとにまとめた理由。"""

    THROTTLED = "throttled"
    SERVICE_UNAVAILABLE = "service_unavailable"
    REQUEST_REJECTED = "request_rejected"
    UNKNOWN = "unknown"


class SqsResponseField(StrEnum):
    """応答の不正箇所を、外部の値を含まないSQSの項目名で表す。"""

    RESPONSE = "response"
    SUCCESSFUL_ENTRIES = "successful_entries"
    FAILED_ENTRIES = "failed_entries"
    SUCCESSFUL_ENTRY = "successful_entry"
    FAILED_ENTRY = "failed_entry"
    ENTRY_ID = "entry_id"
    MESSAGE_ID = "message_id"
    BODY_CHECKSUM = "body_checksum"
    ERROR_CODE = "error_code"
    SENDER_FAULT = "sender_fault"


class SqsResponseInvalidReason(StrEnum):
    """応答の形式または送ったIDとの対応が不正な理由。"""

    INVALID_TYPE = "invalid_type"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    EMPTY_REQUIRED_FIELD = "empty_required_field"
    INVALID_CHECKSUM_FORMAT = "invalid_checksum_format"
    UNKNOWN_ENTRY_ID = "unknown_entry_id"
    DUPLICATE_ENTRY_ID = "duplicate_entry_id"
    MISSING_ENTRY_ID = "missing_entry_id"


class MessageSendError(ApplicationError):
    """送れなかった失敗の基底で、再送するかは受け取った工程が事実から決める。"""


class MessageTooLargeError(MessageSendError):
    """本文がSQSの1件の上限を超えていて、送る前に止めた。"""

    def __init__(self) -> None:
        super().__init__("メッセージの本文が送信できる大きさを超えています")


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


class SqsResponseInvalidError(MessageSendError):
    """SQSの応答の形式または送ったIDとの対応が不正で、受け付けたかを確かめられない。"""

    def __init__(
        self, *, reason: SqsResponseInvalidReason, field: SqsResponseField
    ) -> None:
        super().__init__(
            "SQSの応答を検証できませんでした",
            details={"reason": reason.value, "field": field.value},
        )
        self.reason = reason
        self.field = field


class MessageBodyMismatchError(MessageSendError):
    """SQSが受け取った本文のMD5が、送った本文のMD5と一致しない。"""

    def __init__(self) -> None:
        super().__init__("SQSが受け取った本文が送信した本文と一致しません")


class MessageSendUnexpectedError(MessageSendError):
    """どの分類にも当たらない例外で送れなかった。"""

    def __init__(self, *, exception_type: str) -> None:
        super().__init__(
            "想定外の理由でメッセージを送信できませんでした",
            details={"exception_type": exception_type},
        )


# 既存SQS送信の対応表に合わせ、既知の拒否理由はHTTP statusより優先する。
_REJECTED_CODES = frozenset(
    {
        "InvalidClientTokenId",
        "MissingAuthenticationToken",
        "AccessDenied",
        "AccessDeniedException",
        "NotAuthorized",
        "KmsAccessDenied",
        "OptInRequired",
        "QueueDoesNotExist",
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
        "InvalidSecurity",
        "IncompleteSignature",
        "RequestExpired",
        "KmsDisabled",
        "KmsInvalidState",
        "KmsNotFound",
        "KmsOptInRequired",
        "KmsInvalidKeyUsage",
    }
)
_THROTTLED_CODES = frozenset(
    {"RequestThrottled", "KmsThrottled", "ThrottlingException"}
)
_UNAVAILABLE_CODES = frozenset({"InternalFailure", "ServiceUnavailable"})


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
        raw_code = error.get("Code") if isinstance(error, dict) else None
        code = (
            raw_code
            if isinstance(raw_code, str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", raw_code)
            else None
        )
        metadata = response.get("ResponseMetadata")
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        if code in _REJECTED_CODES:
            reason = SqsServiceErrorReason.REQUEST_REJECTED
        elif code in _THROTTLED_CODES:
            reason = SqsServiceErrorReason.THROTTLED
        elif code in _UNAVAILABLE_CODES or (
            type(status) is int and 500 <= status <= 599
        ):
            reason = SqsServiceErrorReason.SERVICE_UNAVAILABLE
        else:
            reason = SqsServiceErrorReason.UNKNOWN
        return SqsServiceError(reason=reason, service_code=code)
    transport = classify_botocore(exc)
    if transport is not None:
        return MessageSendTransportError(
            failure=transport, exception_type=exception_type
        )
    return MessageSendUnexpectedError(exception_type=exception_type)
