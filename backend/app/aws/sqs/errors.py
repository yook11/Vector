"""SQS送信で起きた事実を分類し、呼び出し元の再送方針は持たない。"""

import re
from dataclasses import dataclass
from enum import StrEnum

from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)

from app.http.failure import (
    HttpTransportFailure,
    classify_botocore,
)
from app.logfire.exceptions import VectorDomainError


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


@dataclass(frozen=True, slots=True)
class SqsSendFailure:
    kind: SqsSendFailureKind
    service_code: str | None = None
    exception_type: str | None = None
    transport: HttpTransportFailure | None = None


class SqsSendError(VectorDomainError):
    def __init__(self, failure: SqsSendFailure) -> None:
        super().__init__()
        self.failure = failure


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


def classify_sqs_send_failure(exc: Exception) -> SqsSendFailure:
    """自由文を保持せず、SDK例外を通信・サービス・設定の事実へ変換する。"""
    if isinstance(exc, SqsSendError):
        return exc.failure
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
        return SqsSendFailure(kind, service_code=code, exception_type=exception_type)
    else:
        transport = classify_botocore(exc)
        if transport is not None:
            return SqsSendFailure(
                SqsSendFailureKind.TRANSPORT,
                exception_type=exception_type,
                transport=transport,
            )
    return SqsSendFailure(kind, exception_type=exception_type)
