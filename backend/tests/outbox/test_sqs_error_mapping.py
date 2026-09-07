"""AWSコードの意味と、対象外例外の境界を検証する。"""

import pytest
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    HTTPClientError,
    ParamValidationError,
    ReadTimeoutError,
)

from app.http.failure import HttpTransportFailureKind
from app.outbox.publish_errors import (
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
)
from app.outbox.sqs_error_mapping import publish_error_from_sqs_exception

CASES = [
    ("throttled", "RequestThrottled KmsThrottled ThrottlingException"),
    ("authentication_failed", "InvalidClientTokenId MissingAuthenticationToken"),
    (
        "access_denied",
        "AccessDenied AccessDeniedException NotAuthorized "
        "KmsAccessDenied OptInRequired",
    ),
    ("destination_not_found", "QueueDoesNotExist"),
    (
        "request_rejected",
        "InvalidAddress InvalidMessageContents UnsupportedOperation InvalidAction "
        "InvalidParameterCombination InvalidParameterValue InvalidQueryParameter "
        "MalformedQueryString MissingAction MissingParameter ValidationError",
    ),
    ("security_rejected", "InvalidSecurity IncompleteSignature"),
    ("request_expired", "RequestExpired"),
    (
        "encryption_error",
        "KmsDisabled KmsInvalidState KmsNotFound KmsOptInRequired KmsInvalidKeyUsage",
    ),
    ("service_unavailable", "InternalFailure ServiceUnavailable"),
]


@pytest.mark.parametrize(
    ("reason", "code"),
    [(reason, code) for reason, codes in CASES for code in codes.split()],
)
def test_known_codes_take_precedence_over_status(reason: str, code: str) -> None:
    """既知のコードはHTTP statusによる推測より優先する。"""
    failure = publish_error_from_sqs_exception(
        ClientError(
            {
                "Error": {"Code": code, "Message": "private"},
                "ResponseMetadata": {"HTTPStatusCode": 503, "RequestId": "request-id"},
            },
            "SendMessage",
        )
    )
    assert isinstance(failure, PublishServiceError)
    assert failure.reason == reason
    assert failure.service_error_code == code
    assert failure.status_code == 503
    assert failure.request_id == "request-id"


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503, None])
def test_unknown_code_uses_only_server_failure_fallback(status: int | None) -> None:
    failure = publish_error_from_sqs_exception(
        ClientError(
            {
                "Error": {"Code": "FutureError"},
                "ResponseMetadata": {"HTTPStatusCode": status},
            },
            "SendMessage",
        )
    )
    assert isinstance(failure, PublishServiceError)
    assert failure.reason == (
        PublishServiceReason.SERVICE_UNAVAILABLE
        if status and status >= 500
        else PublishServiceReason.UNCLASSIFIED
    )


@pytest.mark.parametrize(
    "metadata", [None, "bad", {}, {"HTTPStatusCode": True, "RequestId": 12}]
)
def test_invalid_optional_metadata_does_not_hide_service_response(
    metadata: object,
) -> None:
    exc = ClientError({"Error": {"Code": "ExpiredToken"}}, "SendMessage")
    exc.response["ResponseMetadata"] = metadata
    failure = publish_error_from_sqs_exception(exc)
    assert isinstance(failure, PublishServiceError)
    assert failure.reason is PublishServiceReason.UNCLASSIFIED
    assert failure.status_code is None
    assert failure.request_id is None


@pytest.mark.parametrize(
    "error", [None, {}, {"Code": None}, {"Code": ""}, {"Code": 12}]
)
def test_malformed_service_code_is_unclassified_exception(error: object) -> None:
    exc = ClientError({"Error": {"Code": "placeholder"}}, "SendMessage")
    exc.response["Error"] = error
    assert publish_error_from_sqs_exception(exc) is None


@pytest.mark.parametrize(
    "exc",
    [
        ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole"),
        ParamValidationError(report="private"),
        BotoCoreError(),
        ValueError("private"),
    ],
)
def test_non_sqs_and_non_transport_exceptions_are_not_guessed(exc: Exception) -> None:
    assert publish_error_from_sqs_exception(exc) is None


@pytest.mark.parametrize(
    ("exc", "kind", "reached"),
    [
        (
            ConnectTimeoutError(endpoint_url="https://example.com"),
            HttpTransportFailureKind.CONNECT_TIMEOUT,
            False,
        ),
        (
            ReadTimeoutError(endpoint_url="https://example.com"),
            HttpTransportFailureKind.READ_TIMEOUT,
            True,
        ),
        (HTTPClientError(error="private"), HttpTransportFailureKind.UNKNOWN, True),
    ],
)
def test_transport_classification_preserves_delivery_uncertainty(
    exc, kind, reached
) -> None:
    failure = publish_error_from_sqs_exception(exc)
    assert isinstance(failure, PublishTransportError)
    assert failure.failure.kind is kind
    assert failure.failure.request_may_have_reached_server is reached
