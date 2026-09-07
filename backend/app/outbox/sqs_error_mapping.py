"""SQS送信時のSDK例外をpublisherの失敗契約へ変換する。"""

from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)

from app.http.failure import classify_botocore
from app.outbox.publish_errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
)

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


def configuration_error_from_sdk_exception(
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


def publish_error_from_sqs_exception(exc: Exception) -> PublishError | None:
    """資格情報確定後のSQS送信失敗を分類し、対象外はNoneを返す。"""
    configuration = configuration_error_from_sdk_exception(exc)
    if configuration is not None:
        return configuration
    if isinstance(exc, ClientError):
        if exc.operation_name != "SendMessage" or not isinstance(exc.response, dict):
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
        reason = _REASON_BY_CODE.get(code)
        if reason is None:
            reason = (
                PublishServiceReason.SERVICE_UNAVAILABLE
                if status is not None and status >= 500
                else PublishServiceReason.UNCLASSIFIED
            )
        return PublishServiceError(
            reason=reason,
            service_error_code=code,
            status_code=status,
            request_id=request_id,
        )
    failure = classify_botocore(exc)
    return PublishTransportError(failure=failure) if failure is not None else None
