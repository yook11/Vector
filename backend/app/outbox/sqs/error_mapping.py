"""例外とSQSの個別失敗をpublisherの失敗契約へ変換する。"""

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)

from app.http.failure import classify_botocore
from app.outbox.publishing.errors import (
    PublishCleanupError,
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishPhase,
    PublishResponseInvalidError,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)
from app.outbox.sqs.response_errors import InvalidSqsBatchResponse

_SERVICE_CODES = {
    PublishServiceReason.THROTTLED: (
        "RequestThrottled",
        "KmsThrottled",
        "ThrottlingException",
    ),
    PublishServiceReason.AUTHENTICATION_FAILED: (
        "InvalidClientTokenId",
        "MissingAuthenticationToken",
    ),
    PublishServiceReason.ACCESS_DENIED: (
        "AccessDenied",
        "AccessDeniedException",
        "NotAuthorized",
        "KmsAccessDenied",
        "OptInRequired",
    ),
    PublishServiceReason.DESTINATION_NOT_FOUND: ("QueueDoesNotExist",),
    PublishServiceReason.REQUEST_REJECTED: (
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
    PublishServiceReason.SECURITY_REJECTED: ("InvalidSecurity", "IncompleteSignature"),
    PublishServiceReason.REQUEST_EXPIRED: ("RequestExpired",),
    PublishServiceReason.ENCRYPTION_ERROR: (
        "KmsDisabled",
        "KmsInvalidState",
        "KmsNotFound",
        "KmsOptInRequired",
        "KmsInvalidKeyUsage",
    ),
    PublishServiceReason.SERVICE_UNAVAILABLE: ("InternalFailure", "ServiceUnavailable"),
}
_REASON_BY_CODE = {
    code: reason for reason, codes in _SERVICE_CODES.items() for code in codes
}


class SqsBatchEntryError(Exception):
    """個別失敗の分類に失敗した際の、自由文を含まない原因情報。"""

    def __init__(self, *, code: str, request_id: str | None) -> None:
        super().__init__()
        self.code = code
        self.request_id = request_id


def _configuration_error_from_sdk_exception(
    exc: Exception,
) -> PublishConfigurationError | None:
    """設定不足として確定できるSDK例外だけを変換する。"""
    for exception_type, reason in (
        (NoCredentialsError, PublishConfigurationReason.MISSING_CREDENTIALS),
        (PartialCredentialsError, PublishConfigurationReason.INCOMPLETE_CREDENTIALS),
        (NoRegionError, PublishConfigurationReason.MISSING_REGION),
    ):
        if isinstance(exc, exception_type):
            return PublishConfigurationError(reason=reason)
    return None


def _publish_error_from_sqs_exception(exc: Exception) -> PublishError | None:
    """資格情報確定後のSQS送信失敗を分類し、対象外はNoneを返す。"""
    configuration = _configuration_error_from_sdk_exception(exc)
    if configuration is not None:
        return configuration
    if isinstance(exc, ClientError):
        if exc.operation_name != "SendMessageBatch" or not isinstance(
            exc.response, dict
        ):
            return None
        error = exc.response.get("Error")
        if not isinstance(error, dict):
            return None
        code = error.get("Code")
        if not isinstance(code, str) or not code:
            return None
        metadata = exc.response.get("ResponseMetadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        status = metadata.get("HTTPStatusCode")
        status = status if type(status) is int and 100 <= status <= 599 else None
        request_id = metadata.get("RequestId")
        request_id = request_id if isinstance(request_id, str) else None
        return _service_error_from_sqs_code(
            code=code, status_code=status, request_id=request_id
        )
    failure = classify_botocore(exc)
    return PublishTransportError(failure=failure) if failure is not None else None


def _service_error_from_sqs_code(
    *, code: str, status_code: int | None, request_id: str | None
) -> PublishServiceError:
    """全体応答と個別応答のコードに同じ共通理由を適用する。"""
    reason = _REASON_BY_CODE.get(code)
    if reason is None:
        reason = (
            PublishServiceReason.SERVICE_UNAVAILABLE
            if status_code is not None and status_code >= 500
            else PublishServiceReason.UNCLASSIFIED
        )
    return PublishServiceError(
        reason=reason,
        service_error_code=code,
        status_code=status_code,
        request_id=request_id,
    )


def publish_error_from_exception(
    exc: Exception, *, phase: PublishPhase
) -> PublishError:
    """発生段階の分類と想定外への変換を行い、元の原因を保持する。"""
    if isinstance(exc, PublishError):
        return exc
    try:
        failure = _classify_exception(exc, phase=phase)
    except Exception as classification_exc:
        failure = PublishUnexpectedError(
            original_exception=exc,
            phase=PublishPhase.CLASSIFY_FAILURE,
            classification_exception=classification_exc,
        )
    if failure is None:
        failure = PublishUnexpectedError(original_exception=exc, phase=phase)
    failure.__cause__ = exc
    return failure


def publish_error_from_sqs_entry(*, code: str, request_id: str | None) -> PublishError:
    """検証済みの個別失敗を変換し、分類処理の失敗も共通契約に収める。"""
    try:
        return _service_error_from_sqs_code(
            code=code, status_code=None, request_id=request_id
        )
    except Exception as classification_exc:
        original = SqsBatchEntryError(code=code, request_id=request_id)
        error = PublishUnexpectedError(
            original_exception=original,
            phase=PublishPhase.CLASSIFY_FAILURE,
            classification_exception=classification_exc,
        )
        error.__cause__ = original
        return error


def _classify_exception(exc: Exception, *, phase: PublishPhase) -> PublishError | None:
    """SDK例外の意味を、実際に失敗した操作の境界で判断する。"""
    if phase is PublishPhase.RESOLVE_CREDENTIALS:
        configuration = _configuration_error_from_sdk_exception(exc)
        if configuration is not None:
            return configuration
        if isinstance(exc, (BotoCoreError, ClientError)):
            return PublishConfigurationError(
                reason=PublishConfigurationReason.CREDENTIALS_RETRIEVAL_FAILED
            )
    elif phase is PublishPhase.INITIALIZE:
        return _configuration_error_from_sdk_exception(exc)
    elif phase is PublishPhase.SEND:
        if isinstance(exc, InvalidSqsBatchResponse):
            return PublishResponseInvalidError(reason=exc.reason)
        return _publish_error_from_sqs_exception(exc)
    return None


def publish_cleanup_error_from_exception(exc: Exception) -> PublishCleanupError:
    """終了失敗を送信失敗から分離し、元の原因を保持する。"""
    if isinstance(exc, PublishCleanupError):
        return exc
    error = PublishCleanupError(original_exception=exc)
    error.__cause__ = exc
    return error
