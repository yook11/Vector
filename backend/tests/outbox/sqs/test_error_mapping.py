"""AWSコードの意味と、対象外例外の境界を検証する。"""

from asyncio import CancelledError
from unittest.mock import Mock

import pytest
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    HTTPClientError,
    NoCredentialsError,
    NoRegionError,
    ParamValidationError,
    PartialCredentialsError,
    ReadTimeoutError,
)

from app.http.failure import HttpTransportFailureKind
from app.outbox.publishing.errors import (
    PublishCleanupError,
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishPhase,
    PublishResponseInvalidError,
    PublishResponseInvalidReason,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)
from app.outbox.sqs import error_mapping as mapping
from app.outbox.sqs.error_mapping import (
    SqsBatchEntryError,
    publish_cleanup_error_from_exception,
    publish_error_from_exception,
    publish_error_from_sqs_entry,
)
from app.outbox.sqs.response_errors import SqsResponseField

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
        "MalformedQueryString MissingAction MissingParameter ValidationError "
        "BatchEntryIdsNotDistinct BatchRequestTooLong EmptyBatchRequest "
        "InvalidBatchEntryId TooManyEntriesInBatchRequest",
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
    failure = publish_error_from_exception(
        ClientError(
            {
                "Error": {"Code": code, "Message": "private"},
                "ResponseMetadata": {"HTTPStatusCode": 503, "RequestId": "request-id"},
            },
            "SendMessageBatch",
        ),
        phase=PublishPhase.SEND,
    )
    assert isinstance(failure, PublishServiceError)
    assert failure.reason == reason
    assert failure.service_error_code == code
    assert failure.status_code == 503
    assert failure.request_id == "request-id"


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503, None])
def test_unknown_code_uses_only_server_failure_fallback(status: int | None) -> None:
    failure = publish_error_from_exception(
        ClientError(
            {
                "Error": {"Code": "FutureError"},
                "ResponseMetadata": {"HTTPStatusCode": status},
            },
            "SendMessageBatch",
        ),
        phase=PublishPhase.SEND,
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
    exc = ClientError({"Error": {"Code": "ExpiredToken"}}, "SendMessageBatch")
    exc.response["ResponseMetadata"] = metadata
    failure = publish_error_from_exception(exc, phase=PublishPhase.SEND)
    assert isinstance(failure, PublishServiceError)
    assert failure.reason is PublishServiceReason.UNCLASSIFIED
    assert failure.status_code is None
    assert failure.request_id is None


@pytest.mark.parametrize(
    "error", [None, {}, {"Code": None}, {"Code": ""}, {"Code": 12}]
)
def test_malformed_service_code_becomes_unexpected_failure(error: object) -> None:
    exc = ClientError({"Error": {"Code": "placeholder"}}, "SendMessageBatch")
    exc.response["Error"] = error
    failure = publish_error_from_exception(exc, phase=PublishPhase.SEND)
    assert isinstance(failure, PublishUnexpectedError)
    assert failure.phase is PublishPhase.SEND
    assert failure.__cause__ is exc


@pytest.mark.parametrize(
    "exc",
    [
        ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole"),
        ClientError({"Error": {"Code": "AccessDenied"}}, "SendMessage"),
        ParamValidationError(report="private"),
        BotoCoreError(),
        ValueError("private"),
    ],
)
def test_non_sqs_and_non_transport_exceptions_are_not_guessed(exc: Exception) -> None:
    failure = publish_error_from_exception(exc, phase=PublishPhase.SEND)
    assert isinstance(failure, PublishUnexpectedError)
    assert failure.phase is PublishPhase.SEND
    assert failure.__cause__ is exc


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
    failure = publish_error_from_exception(exc, phase=PublishPhase.SEND)
    assert isinstance(failure, PublishTransportError)
    assert failure.failure.kind is kind
    assert failure.failure.request_may_have_reached_server is reached


@pytest.mark.parametrize(
    ("reason", "code"),
    [(reason, code) for reason, codes in CASES for code in codes.split()],
)
def test_entry_codes_share_service_reasons_without_http_status(reason, code):
    """個別応答でも共通reasonを使い、個別に存在しないstatusを補わない。"""
    error = publish_error_from_sqs_entry(code=code, request_id="batch")
    assert isinstance(error, PublishServiceError)
    assert error.reason == reason
    assert error.service_error_code == code
    assert error.status_code is None
    assert error.request_id == "batch"


@pytest.mark.parametrize("request_id", [None, "PRIVATE_REQUEST_ID"])
def test_unknown_entry_code_keeps_diagnostics_without_exposing_them(request_id):
    """個別応答の未知コードは想定外例外と区別し、調査情報を通常表示しない。"""
    error = publish_error_from_sqs_entry(
        code="PRIVATE_UNKNOWN_CODE", request_id=request_id
    )
    assert isinstance(error, PublishServiceError)
    assert error.reason is PublishServiceReason.UNCLASSIFIED
    assert error.service_error_code == "PRIVATE_UNKNOWN_CODE"
    assert error.request_id == request_id
    assert error.status_code is None
    assert "PRIVATE" not in str(error)
    assert "PRIVATE" not in repr(error)


def test_entry_classifier_failure_keeps_cause_and_classifier_type(monkeypatch):
    """分類処理の障害を変換入口で受け止め、個別応答の調査情報を失わない。"""
    monkeypatch.setattr(
        mapping,
        "_service_error_from_sqs_code",
        Mock(side_effect=ValueError("PRIVATE_CLASSIFIER")),
    )
    error = publish_error_from_sqs_entry(
        code="PRIVATE_CODE", request_id="PRIVATE_REQUEST_ID"
    )
    assert isinstance(error, PublishUnexpectedError)
    assert error.phase is PublishPhase.CLASSIFY_FAILURE
    assert error.original_exception_type == (
        "app.outbox.sqs.error_mapping.SqsBatchEntryError"
    )
    assert error.classification_exception_type == "builtins.ValueError"
    assert isinstance(error.__cause__, SqsBatchEntryError)
    assert error.__cause__.code == "PRIVATE_CODE"
    assert error.__cause__.request_id == "PRIVATE_REQUEST_ID"
    assert error.__cause__.args == ()
    for exception in (error, error.__cause__):
        assert "PRIVATE" not in str(exception)
        assert "PRIVATE" not in repr(exception)


@pytest.mark.parametrize(
    "exit_error", [KeyboardInterrupt(), SystemExit(), CancelledError()]
)
def test_entry_classifier_exit_is_not_converted(exit_error, monkeypatch):
    """個別応答の分類中もキャンセルやプロセス終了を送信失敗へ変換しない。"""
    monkeypatch.setattr(
        mapping, "_service_error_from_sqs_code", Mock(side_effect=exit_error)
    )
    with pytest.raises(type(exit_error)) as caught:
        publish_error_from_sqs_entry(code="RequestThrottled", request_id=None)
    assert caught.value is exit_error


@pytest.mark.parametrize(
    ("phase", "expected_type", "reason"),
    [
        (
            PublishPhase.RESOLVE_CREDENTIALS,
            PublishConfigurationError,
            PublishConfigurationReason.CREDENTIALS_RETRIEVAL_FAILED,
        ),
        (PublishPhase.SEND, PublishTransportError, None),
        (PublishPhase.INITIALIZE, PublishUnexpectedError, None),
        (PublishPhase.PREPARE_EVENT, PublishUnexpectedError, None),
        (None, PublishCleanupError, None),
    ],
)
def test_same_timeout_is_classified_by_operation(phase, expected_type, reason):
    """同じ通信例外でも資格情報取得・送信・終了を混同しない。"""
    original = ReadTimeoutError(endpoint_url="PRIVATE_URL")
    error = (
        publish_cleanup_error_from_exception(original)
        if phase is None
        else publish_error_from_exception(original, phase=phase)
    )
    assert isinstance(error, expected_type)
    assert error.__cause__ is original
    if isinstance(error, PublishConfigurationError):
        assert error.reason is reason
    elif isinstance(error, PublishUnexpectedError):
        assert error.phase is phase
    elif isinstance(error, PublishCleanupError):
        assert not isinstance(error, PublishError)
    else:
        assert error.failure.kind is HttpTransportFailureKind.READ_TIMEOUT


@pytest.mark.parametrize("phase", list(PublishPhase))
def test_unclassified_exception_retains_phase_and_private_cause(phase):
    """分類不能でも元の段階と原因を保ち、自由文を公開しない。"""
    original = ValueError("PRIVATE_BODY_QUEUE_CREDENTIAL")
    error = publish_error_from_exception(original, phase=phase)
    assert isinstance(error, PublishUnexpectedError)
    assert vars(error) == {
        "original_exception_type": "builtins.ValueError",
        "phase": phase,
        "classification_exception_type": None,
    }
    assert error.__cause__ is original
    assert "PRIVATE" not in str(error)
    assert "PRIVATE" not in repr(error)


@pytest.mark.parametrize("phase", [*PublishPhase, None])
def test_existing_publish_error_keeps_identity_except_during_cleanup(phase):
    """確定済みの原因を保持し、終了時だけ送信結果から分離する。"""
    original = PublishConfigurationError(
        reason=PublishConfigurationReason.MISSING_CREDENTIALS
    )
    cause = RuntimeError("private")
    original.__cause__ = cause
    error = (
        publish_cleanup_error_from_exception(original)
        if phase is None
        else publish_error_from_exception(original, phase=phase)
    )
    if phase is None:
        assert isinstance(error, PublishCleanupError)
        assert not isinstance(error, PublishError)
        assert error.__cause__ is original
    else:
        assert error is original
    assert original.__cause__ is cause


@pytest.mark.parametrize(
    "phase",
    [PublishPhase.INITIALIZE, PublishPhase.RESOLVE_CREDENTIALS, PublishPhase.SEND],
)
@pytest.mark.parametrize(
    ("original", "reason"),
    [
        (NoCredentialsError(), PublishConfigurationReason.MISSING_CREDENTIALS),
        (
            PartialCredentialsError(provider="test", cred_var="private"),
            PublishConfigurationReason.INCOMPLETE_CREDENTIALS,
        ),
        (NoRegionError(), PublishConfigurationReason.MISSING_REGION),
    ],
)
def test_specific_configuration_reason_takes_precedence(phase, original, reason):
    """確定できる設定不足は、一般的な資格情報取得失敗より優先する。"""
    error = publish_error_from_exception(original, phase=phase)
    assert isinstance(error, PublishConfigurationError)
    assert error.reason is reason
    assert error.__cause__ is original


@pytest.mark.parametrize("phase", [PublishPhase.RESOLVE_CREDENTIALS, PublishPhase.SEND])
def test_sts_response_is_never_classified_as_sqs_response(phase):
    """STSの権限応答をSQSサービスの権限不足へ分類しない。"""
    original = ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole")
    error = publish_error_from_exception(original, phase=phase)
    if phase is PublishPhase.RESOLVE_CREDENTIALS:
        assert isinstance(error, PublishConfigurationError)
        assert error.reason is PublishConfigurationReason.CREDENTIALS_RETRIEVAL_FAILED
    else:
        assert isinstance(error, PublishUnexpectedError)
        assert error.phase is PublishPhase.SEND
    assert error.__cause__ is original


@pytest.mark.parametrize(
    ("phase", "classifier"),
    [
        (PublishPhase.INITIALIZE, "_configuration_error_from_sdk_exception"),
        (PublishPhase.RESOLVE_CREDENTIALS, "_configuration_error_from_sdk_exception"),
        (PublishPhase.SEND, "_publish_error_from_sqs_exception"),
    ],
)
def test_classifier_failure_keeps_original_and_classifier_types(
    phase, classifier, monkeypatch
):
    """各操作の分類処理が壊れても、元の障害と分類失敗の型を保持する。"""
    original = RuntimeError("PRIVATE_ORIGINAL")
    monkeypatch.setattr(
        mapping, classifier, Mock(side_effect=ValueError("PRIVATE_CLASSIFIER"))
    )
    error = publish_error_from_exception(original, phase=phase)
    assert isinstance(error, PublishUnexpectedError)
    assert vars(error) == {
        "original_exception_type": "builtins.RuntimeError",
        "phase": PublishPhase.CLASSIFY_FAILURE,
        "classification_exception_type": "builtins.ValueError",
    }
    assert error.__cause__ is original
    assert "PRIVATE" not in repr(error)


def test_classifier_process_exit_is_not_converted(monkeypatch):
    """分類中のプロセス終了は通常の分類失敗として捕捉しない。"""
    exit_error = KeyboardInterrupt()
    monkeypatch.setattr(
        mapping, "_publish_error_from_sqs_exception", Mock(side_effect=exit_error)
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        publish_error_from_exception(RuntimeError(), phase=PublishPhase.SEND)
    assert caught.value is exit_error


@pytest.mark.parametrize("reason", list(PublishResponseInvalidReason))
def test_response_violation_keeps_sqs_field_only_in_cause(reason):
    """検出済みの応答違反を想定外へ落とさず、共通契約と原因を保持する。"""
    from app.outbox.sqs.response_errors import InvalidSqsBatchResponse

    original = InvalidSqsBatchResponse(reason=reason, field=SqsResponseField.ENTRY_ID)
    error = publish_error_from_exception(original, phase=PublishPhase.SEND)
    assert isinstance(error, PublishResponseInvalidError)
    assert error.reason is reason
    assert not hasattr(error, "field")
    assert original.field is SqsResponseField.ENTRY_ID
    assert error.__cause__ is original
    assert publish_error_from_exception(error, phase=PublishPhase.SEND) is error


@pytest.mark.parametrize(
    "phase", [p for p in PublishPhase if p is not PublishPhase.SEND]
)
def test_response_violation_outside_send_remains_unexpected(phase):
    """送信以外の境界で発生した応答違反を送信先の応答として解釈しない。"""
    from app.outbox.sqs.response_errors import InvalidSqsBatchResponse

    original = InvalidSqsBatchResponse(
        reason=PublishResponseInvalidReason.INVALID_TYPE,
        field=SqsResponseField.RESPONSE,
    )
    error = publish_error_from_exception(original, phase=phase)
    assert isinstance(error, PublishUnexpectedError)
    assert error.phase is phase
    assert error.__cause__ is original


def test_response_violation_mapping_failure_retains_original(monkeypatch):
    """応答違反の変換自体が失敗しても、元の理由と分類例外型を失わない。"""
    from app.outbox.sqs.response_errors import InvalidSqsBatchResponse

    original = InvalidSqsBatchResponse(
        reason=PublishResponseInvalidReason.INVALID_TYPE,
        field=SqsResponseField.RESPONSE,
    )
    monkeypatch.setattr(
        mapping,
        "PublishResponseInvalidError",
        Mock(side_effect=RuntimeError("PRIVATE")),
    )
    error = publish_error_from_exception(original, phase=PublishPhase.SEND)
    assert isinstance(error, PublishUnexpectedError)
    assert error.phase is PublishPhase.CLASSIFY_FAILURE
    assert error.classification_exception_type == "builtins.RuntimeError"
    assert (
        error.original_exception_type
        == "app.outbox.sqs.response_errors.InvalidSqsBatchResponse"
    )
    assert error.__cause__ is original
    assert original.reason is PublishResponseInvalidReason.INVALID_TYPE
    assert "PRIVATE" not in str(error)
