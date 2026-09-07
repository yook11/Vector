"""送信失敗の情報保持と、例外文面に公開する情報の境界を検証する。"""

import pytest

from app.http.failure import HttpTransportFailure, HttpTransportFailureKind
from app.outbox.publish_errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishEventInvalidError,
    PublishEventInvalidReason,
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


def test_unexpected_error_keeps_diagnostics_without_message() -> None:
    from app.outbox.publish_errors import PublishPhase, PublishUnexpectedError

    error = PublishUnexpectedError(
        original_exception=RuntimeError("private-body-and-credentials"),
        phase=PublishPhase.SEND,
    )
    assert error.reason == "unexpected_exception"
    assert error.original_exception_type == "builtins.RuntimeError"
    assert error.phase is PublishPhase.SEND
    assert error.classification_exception_type is None
    assert "private-body-and-credentials" not in str(error)
    assert "private-body-and-credentials" not in repr(error)
    assert error.args == ()


def test_publish_exception_chain_is_redacted_at_logfire_export(capfire) -> None:
    import logfire
    from botocore.exceptions import ClientError

    from app.logfire.redaction import install_exception_redaction
    from app.outbox.sqs_error_mapping import publish_error_from_sqs_exception

    install_exception_redaction()
    marker = "PRIVATE_PAYLOAD_QUEUE_CREDENTIAL_MARKER"
    with pytest.raises(PublishServiceError):
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
                    "SendMessage",
                )
            except ClientError as exc:
                raise publish_error_from_sqs_exception(exc) from exc
    spans = capfire.exporter.exported_spans
    assert spans
    for span in spans:
        assert marker not in str(span.attributes)
        assert marker not in str(span.status.description)
        for event in span.events:
            assert marker not in str(event.attributes)
