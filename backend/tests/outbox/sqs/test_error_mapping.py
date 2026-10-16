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

from app.aws.sqs.errors import SqsServiceError, SqsServiceErrorReason
from app.aws.sqs.outgoing_message import (
    MessageBodyMismatchError,
    SqsResponseField,
    SqsResponseInvalidError,
    SqsResponseInvalidReason,
)
from app.http.failure import HttpTransportFailureReason, HttpTransportStage
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
from app.outbox.sqs import error_mapping as mapping
from app.outbox.sqs.error_mapping import (
    publish_cleanup_error_from_exception,
    publish_error_from_exception,
)

# SQSの理由と送信の失敗の理由は同じ分け方で、SQSの未知だけを送信の未分類に写す。
SERVICE_REASONS = [
    (SqsServiceErrorReason.THROTTLED, PublishServiceReason.THROTTLED),
    (
        SqsServiceErrorReason.SERVICE_UNAVAILABLE,
        PublishServiceReason.SERVICE_UNAVAILABLE,
    ),
    (
        SqsServiceErrorReason.AUTHENTICATION_FAILED,
        PublishServiceReason.AUTHENTICATION_FAILED,
    ),
    (SqsServiceErrorReason.ACCESS_DENIED, PublishServiceReason.ACCESS_DENIED),
    (
        SqsServiceErrorReason.DESTINATION_NOT_FOUND,
        PublishServiceReason.DESTINATION_NOT_FOUND,
    ),
    (SqsServiceErrorReason.REQUEST_REJECTED, PublishServiceReason.REQUEST_REJECTED),
    (SqsServiceErrorReason.SECURITY_REJECTED, PublishServiceReason.SECURITY_REJECTED),
    (SqsServiceErrorReason.REQUEST_EXPIRED, PublishServiceReason.REQUEST_EXPIRED),
    (SqsServiceErrorReason.ENCRYPTION_ERROR, PublishServiceReason.ENCRYPTION_ERROR),
    (SqsServiceErrorReason.UNKNOWN, PublishServiceReason.UNCLASSIFIED),
]


def test_every_sqs_service_reason_has_a_publish_reason():
    """SQSの理由をすべて送信の失敗の理由へ写し、写し先のない理由を残さない。"""
    assert [sqs for sqs, _ in SERVICE_REASONS] == list(SqsServiceErrorReason)


@pytest.mark.parametrize(("reason", "expected"), SERVICE_REASONS)
def test_sqs_rejection_becomes_publish_service_error(reason, expected):
    """SQSが拒否した理由を同じ意味の送信の理由へ写し、元のコードと原因を保つ。"""
    original = SqsServiceError(reason=reason, service_code="Code")
    error = publish_error_from_exception(original, phase=PublishPhase.SEND)
    assert isinstance(error, PublishServiceError)
    assert (
        error.reason,
        error.service_error_code,
        error.status_code,
        error.request_id,
    ) == (expected, "Code", None, None)
    assert error.__cause__ is original


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("AccessDenied", PublishServiceReason.ACCESS_DENIED),
        ("RequestThrottled", PublishServiceReason.THROTTLED),
    ],
)
def test_known_codes_take_precedence_over_status(code, reason) -> None:
    """呼び出し全体の失敗では既知のコードをstatusより優先し、statusとRequestIdを保つ。"""
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
    assert failure.reason is reason
    assert failure.service_error_code == code
    assert failure.status_code == 503
    assert failure.request_id == "request-id"


def test_malformed_but_present_code_is_unclassified_without_keeping_it() -> None:
    """呼び出し全体の失敗のコードが形として不正なら、理由を推測せずコードも持たない。"""
    failure = publish_error_from_exception(
        ClientError(
            {
                "Error": {"Code": "Access Denied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "SendMessageBatch",
        ),
        phase=PublishPhase.SEND,
    )
    assert isinstance(failure, PublishServiceError)
    assert (failure.reason, failure.service_error_code) == (
        PublishServiceReason.UNCLASSIFIED,
        None,
    )


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
    ("exc", "stage", "reason", "reached"),
    [
        (
            ConnectTimeoutError(endpoint_url="https://example.com"),
            HttpTransportStage.CONNECT,
            HttpTransportFailureReason.TIMEOUT,
            False,
        ),
        (
            ReadTimeoutError(endpoint_url="https://example.com"),
            HttpTransportStage.RECEIVE,
            HttpTransportFailureReason.TIMEOUT,
            True,
        ),
        (
            HTTPClientError(error="private"),
            HttpTransportStage.UNKNOWN,
            HttpTransportFailureReason.UNKNOWN,
            True,
        ),
    ],
)
def test_transport_classification_preserves_delivery_uncertainty(
    exc, stage, reason, reached
) -> None:
    failure = publish_error_from_exception(exc, phase=PublishPhase.SEND)
    assert isinstance(failure, PublishTransportError)
    assert failure.failure.stage is stage
    assert failure.failure.reason is reason
    assert failure.failure.request_may_have_reached_server is reached


def test_body_mismatch_becomes_integrity_failure():
    """SQSが受け取った本文の不一致を、受付済みの可能性を残す整合性の失敗にする。"""
    original = MessageBodyMismatchError()
    error = publish_error_from_exception(original, phase=PublishPhase.SEND)
    assert isinstance(error, PublishIntegrityError)
    assert error.reason is PublishIntegrityReason.BODY_CHECKSUM_MISMATCH
    assert error.__cause__ is original


def test_rejection_mapping_failure_keeps_cause_and_classifier_type(monkeypatch):
    """SQSの拒否の変換が壊れても、元の拒否と分類例外の型を失わない。"""
    monkeypatch.setattr(
        mapping,
        "_publish_service_error",
        Mock(side_effect=ValueError("PRIVATE_CLASSIFIER")),
    )
    original = SqsServiceError(
        reason=SqsServiceErrorReason.ACCESS_DENIED, service_code="AccessDenied"
    )
    error = publish_error_from_exception(original, phase=PublishPhase.SEND)
    assert isinstance(error, PublishUnexpectedError)
    assert vars(error) == {
        "original_exception_type": "app.aws.sqs.errors.SqsServiceError",
        "phase": PublishPhase.CLASSIFY_FAILURE,
        "classification_exception_type": "builtins.ValueError",
    }
    assert error.__cause__ is original
    assert "PRIVATE" not in str(error)
    assert "PRIVATE" not in repr(error)


@pytest.mark.parametrize(
    "exit_error", [KeyboardInterrupt(), SystemExit(), CancelledError()]
)
def test_rejection_mapping_exit_is_not_converted(exit_error, monkeypatch):
    """SQSの拒否の変換中も、キャンセルやプロセス終了を送信失敗へ変換しない。"""
    monkeypatch.setattr(mapping, "_publish_service_error", Mock(side_effect=exit_error))
    original = SqsServiceError(
        reason=SqsServiceErrorReason.THROTTLED, service_code="RequestThrottled"
    )
    with pytest.raises(type(exit_error)) as caught:
        publish_error_from_exception(original, phase=PublishPhase.SEND)
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
        assert error.failure.stage is HttpTransportStage.RECEIVE
        assert error.failure.reason is HttpTransportFailureReason.TIMEOUT


@pytest.mark.parametrize(
    "phase",
    [phase for phase in PublishPhase if phase is not PublishPhase.PREPARE_EVENT],
)
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


@pytest.mark.parametrize(
    "phase",
    [phase for phase in PublishPhase if phase is not PublishPhase.PREPARE_EVENT]
    + [None],
)
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


def _response_invalid():
    return SqsResponseInvalidError(
        reason=SqsResponseInvalidReason.NOT_IN_REQUEST,
        field=SqsResponseField.ENTRY_ID,
    )


def test_response_violation_keeps_sqs_detail_only_in_cause():
    """検出済みの応答違反を想定外へ落とさず、SQSの不正箇所は原因の例外にだけ残す。"""
    original = _response_invalid()
    error = publish_error_from_exception(original, phase=PublishPhase.SEND)
    assert isinstance(error, PublishResponseInvalidError)
    assert vars(error) == {}
    assert error.__cause__ is original
    assert publish_error_from_exception(error, phase=PublishPhase.SEND) is error


@pytest.mark.parametrize(
    "phase", [p for p in PublishPhase if p is not PublishPhase.SEND]
)
def test_response_violation_outside_send_remains_unexpected(phase):
    """送信以外の境界で発生した応答違反を送信先の応答として解釈しない。"""
    original = _response_invalid()
    error = publish_error_from_exception(original, phase=phase)
    assert isinstance(error, PublishUnexpectedError)
    assert error.phase is phase
    assert error.__cause__ is original


def test_response_violation_mapping_failure_retains_original(monkeypatch):
    """応答違反の変換自体が失敗しても、元の例外と分類例外型を失わない。"""
    original = _response_invalid()
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
        == "app.aws.sqs.outgoing_message.SqsResponseInvalidError"
    )
    assert error.__cause__ is original
    assert "PRIVATE" not in str(error)
