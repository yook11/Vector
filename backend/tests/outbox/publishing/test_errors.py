"""送信失敗の情報保持と、例外文面に公開する情報の境界を検証する。"""

import pytest

from app.http.failure import HttpTransportFailure, HttpTransportFailureKind
from app.outbox.publishing.errors import (
    PublishCleanupError,
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishEventInvalidError,
    PublishEventInvalidReason,
    PublishResponseInvalidError,
    PublishResponseInvalidReason,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
)


@pytest.mark.parametrize(
    ("kind", "may_have_reached"),
    [
        (HttpTransportFailureKind.CONNECT_TIMEOUT, False),
        (HttpTransportFailureKind.READ_TIMEOUT, True),
        (HttpTransportFailureKind.UNKNOWN, True),
    ],
)
def test_publish_transport_error_preserves_classification(
    kind: HttpTransportFailureKind, may_have_reached: bool
) -> None:
    """文脈を送信失敗に変えても通信の分類情報を変更しない。"""
    failure = HttpTransportFailure(kind, may_have_reached)
    with pytest.raises(PublishError) as caught:
        raise PublishTransportError(failure=failure)
    assert caught.value.failure is failure


@pytest.mark.parametrize("status_code", [403, 429, 500, None])
def test_service_error_keeps_code_without_exposing_external_text(
    status_code: int | None,
) -> None:
    """送信先のコードは判断用に保持し、例外文面やargsへ露出させない。"""
    code = "external-text-with-private-data"
    error = PublishServiceError(
        reason=PublishServiceReason.UNCLASSIFIED,
        service_error_code=code,
        status_code=status_code,
        request_id=code,
    )
    assert error.service_error_code == code
    assert error.status_code == status_code
    assert code not in str(error)
    assert code not in repr(error)
    assert error.args == ()


@pytest.mark.parametrize("reason", list(PublishConfigurationReason))
def test_configuration_error_keeps_stable_reason(
    reason: PublishConfigurationReason,
) -> None:
    """資格情報そのものを受け取らず、設定不足の理由だけを公開する。"""
    error = PublishConfigurationError(reason=reason)
    assert isinstance(error, PublishError)
    assert error.reason is reason
    assert reason.value in str(error)
    assert error.args == ()


@pytest.mark.parametrize("reason", list(PublishEventInvalidReason))
def test_event_invalid_error_does_not_require_event_content(
    reason: PublishEventInvalidReason,
) -> None:
    """イベント本体を保持せず、不正と判断した理由を伝える。"""
    error = PublishEventInvalidError(reason=reason)
    assert isinstance(error, PublishError)
    assert error.reason is reason
    assert reason.value in str(error)
    assert error.args == ()


@pytest.mark.parametrize("cleanup", [False, True])
def test_error_keeps_diagnostics_without_message(cleanup) -> None:
    from app.outbox.publishing.errors import PublishPhase, PublishUnexpectedError

    original = RuntimeError("private-body-and-credentials")
    error = (
        PublishCleanupError(original_exception=original)
        if cleanup
        else PublishUnexpectedError(
            original_exception=original, phase=PublishPhase.SEND
        )
    )
    assert error.original_exception_type == "builtins.RuntimeError"
    if cleanup:
        assert not isinstance(error, PublishError)
        assert error.CODE == "publish_cleanup_error"
        assert vars(error) == {"original_exception_type": "builtins.RuntimeError"}
    else:
        assert error.reason == "unexpected_exception"
        assert error.phase is PublishPhase.SEND
        assert error.classification_exception_type is None
    assert "private-body-and-credentials" not in str(error)
    assert "private-body-and-credentials" not in repr(error)
    assert error.args == ()


@pytest.mark.parametrize("cleanup", [False, True])
def test_publish_exception_chain_is_redacted_at_logfire_export(
    capfire, cleanup
) -> None:
    import logfire
    from botocore.exceptions import ClientError

    from app.logfire.redaction import install_exception_redaction
    from app.outbox.publishing.errors import PublishPhase
    from app.outbox.sqs.error_mapping import (
        publish_cleanup_error_from_exception,
        publish_error_from_exception,
    )

    install_exception_redaction()
    marker = "PRIVATE_PAYLOAD_QUEUE_CREDENTIAL_MARKER"
    with pytest.raises(PublishCleanupError if cleanup else PublishServiceError):
        with logfire.span("publish_test"):
            try:
                raise ClientError(
                    {
                        "Error": {"Code": marker, "Message": marker},
                        "ResponseMetadata": {
                            "RequestId": marker,
                            "HTTPStatusCode": 400,
                        },
                    },
                    "SendMessageBatch",
                )
            except ClientError as exc:
                error = (
                    publish_cleanup_error_from_exception(exc)
                    if cleanup
                    else publish_error_from_exception(exc, phase=PublishPhase.SEND)
                )
                raise error from exc
    spans = capfire.exporter.exported_spans
    assert spans
    for span in spans:
        assert marker not in str(span.attributes)
        assert marker not in str(span.status.description)
        for event in span.events:
            assert marker not in str(event.attributes)


def test_integrity_error_preserves_reason_and_diagnostics():
    """本文不一致の識別情報と、保持する調査情報をまとめて確認する。"""
    from app.outbox.publishing.errors import (
        PublishIntegrityError,
        PublishIntegrityReason,
    )

    error = PublishIntegrityError(
        reason=PublishIntegrityReason.BODY_CHECKSUM_MISMATCH, request_id="request-id"
    )
    assert isinstance(error, PublishError)
    assert {"code": error.CODE, "reason": error.reason.value} == {
        "code": "publish_integrity_error",
        "reason": "body_checksum_mismatch",
    }
    assert vars(error) == {
        "reason": PublishIntegrityReason.BODY_CHECKSUM_MISMATCH,
        "request_id": "request-id",
    }


def test_integrity_error_displays_fixed_message_without_private_diagnostics():
    """固定の説明文を表示し、調査情報を例外の通常表示へ露出しない。"""
    from app.outbox.publishing.errors import (
        PublishIntegrityError,
        PublishIntegrityReason,
    )

    marker = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    error = PublishIntegrityError(
        reason=PublishIntegrityReason.BODY_CHECKSUM_MISMATCH, request_id=marker
    )
    error.__cause__ = RuntimeError(marker)
    assert (
        error.MESSAGE
        == "送信本文と、送信先が受け取った本文のチェックサムが一致しません。"
    )
    assert error.MESSAGE in str(error)
    assert error.args == ()
    assert marker not in str(error)
    assert marker not in repr(error)


@pytest.mark.parametrize("reason", list(PublishResponseInvalidReason))
def test_response_invalid_error_exposes_only_fixed_diagnostics(reason):
    """共通の違反理由だけを公開し、SQSの項目や原因文面を表示しない。"""
    error = PublishResponseInvalidError(reason=reason)
    error.__cause__ = ValueError("PRIVATE_BODY_QUEUE_CREDENTIAL")
    assert isinstance(error, PublishError)
    assert error.CODE == "publish_response_invalid"
    assert vars(error) == {"reason": reason}
    assert error.args == ()
    assert error.reason.value in str(error)
    assert "PRIVATE" not in str(error)
    assert "PRIVATE" not in repr(error)


def test_response_invalid_keeps_reason_without_raw_values_at_logfire_export(capfire):
    """応答違反を共通例外へ変換した後も、Logfireへ応答の自由文を渡さない。"""
    import logfire

    from app.logfire.redaction import install_exception_redaction
    from app.outbox.publishing.errors import PublishPhase
    from app.outbox.sqs.batch_response import decode_sqs_batch_response
    from app.outbox.sqs.error_mapping import publish_error_from_exception
    from app.outbox.sqs.response_errors import InvalidSqsBatchResponse, SqsResponseField

    install_exception_redaction()
    marker = "PRIVATE_BODY_QUEUE_CREDENTIAL"
    with pytest.raises(PublishResponseInvalidError) as caught:
        with logfire.span("publish_response_invalid_test"):
            try:
                decode_sqs_batch_response(
                    {"Successful": [{"MD5OfMessageBody": marker}]}
                )
            except InvalidSqsBatchResponse as exc:
                raise publish_error_from_exception(
                    exc, phase=PublishPhase.SEND
                ) from exc
    assert caught.value.reason is PublishResponseInvalidReason.INVALID_CHECKSUM_FORMAT
    assert not hasattr(caught.value, "field")
    assert vars(caught.value.__cause__) == {
        "reason": PublishResponseInvalidReason.INVALID_CHECKSUM_FORMAT,
        "field": SqsResponseField.BODY_CHECKSUM,
    }
    spans = capfire.exporter.exported_spans
    assert spans
    for span in spans:
        assert marker not in str(span.attributes)
        assert marker not in str(span.status.description)
        for event in span.events:
            assert marker not in str(event.attributes)
