"""例外とSQSの個別失敗をpublisherの失敗契約へ変換する。"""

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)

from app.aws.sqs.errors import (
    SqsServiceError,
    SqsServiceErrorReason,
    to_sqs_service_error,
)
from app.aws.sqs.outgoing_message import (
    MessageBodyMismatchError,
    SqsResponseInvalidError,
)
from app.http.failure import classify_botocore
from app.outbox.publishing.errors import (
    PublishCleanupError,
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishIntegrityError,
    PublishIntegrityReason,
    PublishPhase,
    PublishResponseInvalidError,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)

_PUBLISH_SERVICE_REASONS = {
    SqsServiceErrorReason.THROTTLED: PublishServiceReason.THROTTLED,
    SqsServiceErrorReason.SERVICE_UNAVAILABLE: PublishServiceReason.SERVICE_UNAVAILABLE,
    SqsServiceErrorReason.AUTHENTICATION_FAILED: (
        PublishServiceReason.AUTHENTICATION_FAILED
    ),
    SqsServiceErrorReason.ACCESS_DENIED: PublishServiceReason.ACCESS_DENIED,
    SqsServiceErrorReason.DESTINATION_NOT_FOUND: (
        PublishServiceReason.DESTINATION_NOT_FOUND
    ),
    SqsServiceErrorReason.REQUEST_REJECTED: PublishServiceReason.REQUEST_REJECTED,
    SqsServiceErrorReason.SECURITY_REJECTED: PublishServiceReason.SECURITY_REJECTED,
    SqsServiceErrorReason.REQUEST_EXPIRED: PublishServiceReason.REQUEST_EXPIRED,
    SqsServiceErrorReason.ENCRYPTION_ERROR: PublishServiceReason.ENCRYPTION_ERROR,
    SqsServiceErrorReason.UNKNOWN: PublishServiceReason.UNCLASSIFIED,
}


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
        return _publish_service_error(
            to_sqs_service_error(code, status_code=status),
            status_code=status,
            request_id=request_id,
        )
    failure = classify_botocore(exc)
    return PublishTransportError(failure=failure) if failure is not None else None


def _publish_service_error(
    error: SqsServiceError,
    *,
    status_code: int | None = None,
    request_id: str | None = None,
) -> PublishServiceError:
    """SQSが拒否した理由を、送信の失敗の同じ意味の理由へ写す。"""
    return PublishServiceError(
        reason=_PUBLISH_SERVICE_REASONS[error.reason],
        service_error_code=error.service_code,
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
        if isinstance(exc, SqsResponseInvalidError):
            return PublishResponseInvalidError()
        if isinstance(exc, SqsServiceError):
            return _publish_service_error(exc)
        if isinstance(exc, MessageBodyMismatchError):
            return PublishIntegrityError(
                reason=PublishIntegrityReason.BODY_CHECKSUM_MISMATCH
            )
        return _publish_error_from_sqs_exception(exc)
    return None


def publish_cleanup_error_from_exception(exc: Exception) -> PublishCleanupError:
    """終了失敗を送信失敗から分離し、元の原因を保持する。"""
    if isinstance(exc, PublishCleanupError):
        return exc
    error = PublishCleanupError(original_exception=exc)
    error.__cause__ = exc
    return error
