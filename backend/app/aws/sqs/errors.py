"""SQS送信で起きた事実を分類し、送り直してよい失敗かを判定して伝える。"""

import re
from enum import StrEnum

from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)

from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    classify_botocore,
)
from app.shared.errors import ApplicationError


class SqsSendFailureKind(StrEnum):
    TRANSPORT = "transport"
    CONFIGURATION = "configuration"
    THROTTLED = "throttled"
    SERVICE_UNAVAILABLE = "service_unavailable"
    REQUEST_REJECTED = "request_rejected"
    UNKNOWN_SERVICE_ERROR = "unknown_service_error"
    INVALID_RESPONSE = "invalid_response"
    BODY_MISMATCH = "body_mismatch"
    UNEXPECTED = "unexpected"


def _is_retryable(
    kind: SqsSendFailureKind, transport: HttpTransportFailure | None
) -> bool:
    if kind in (SqsSendFailureKind.THROTTLED, SqsSendFailureKind.SERVICE_UNAVAILABLE):
        return True
    if kind is not SqsSendFailureKind.TRANSPORT or transport is None:
        return False
    if transport.reason is HttpTransportFailureReason.PROXY:
        status = transport.proxy_status
        return status is None or status == 429 or 500 <= status <= 599
    return transport.reason in {
        HttpTransportFailureReason.TIMEOUT,
        HttpTransportFailureReason.DNS_RESOLUTION,
        HttpTransportFailureReason.NETWORK_IO,
        HttpTransportFailureReason.PROTOCOL_VIOLATION,
        HttpTransportFailureReason.UNKNOWN,
    }


class MessageSendError(ApplicationError):
    """送れなかった事実と送り直してよいかを持ち、回数と待ち方は呼び出し元が決める。"""

    def __init__(
        self,
        kind: SqsSendFailureKind,
        *,
        service_code: str | None = None,
        exception_type: str | None = None,
        transport: HttpTransportFailure | None = None,
    ) -> None:
        super().__init__(
            "メッセージを送信できませんでした",
            details={
                "failure_kind": kind.value,
                "service_code": service_code,
                "exception_type": exception_type,
                "transport_reason": transport.reason.value if transport else None,
            },
        )
        self.retryable = _is_retryable(kind, transport)


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
    """自由文を保持せず、SDK例外を通信・サービス・設定の事実へ変換する。"""
    if isinstance(exc, MessageSendError):
        return exc
    exception_type = type(exc).__name__
    kind = SqsSendFailureKind.UNEXPECTED
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError, NoRegionError)):
        kind = SqsSendFailureKind.CONFIGURATION
    elif isinstance(exc, ClientError) and exc.operation_name == "SendMessage":
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
            kind = SqsSendFailureKind.REQUEST_REJECTED
        elif code in _THROTTLED_CODES:
            kind = SqsSendFailureKind.THROTTLED
        elif code in _UNAVAILABLE_CODES or (
            type(status) is int and 500 <= status <= 599
        ):
            kind = SqsSendFailureKind.SERVICE_UNAVAILABLE
        else:
            kind = SqsSendFailureKind.UNKNOWN_SERVICE_ERROR
        return MessageSendError(kind, service_code=code, exception_type=exception_type)
    else:
        transport = classify_botocore(exc)
        if transport is not None:
            return MessageSendError(
                SqsSendFailureKind.TRANSPORT,
                exception_type=exception_type,
                transport=transport,
            )
    return MessageSendError(kind, exception_type=exception_type)
