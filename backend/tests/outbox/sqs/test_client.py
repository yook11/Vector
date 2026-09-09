"""資格情報取得先とSQS送信先の失敗を区別する。"""

from unittest.mock import Mock

import botocore.session
import pytest
from botocore.credentials import ReadOnlyCredentials
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
    ReadTimeoutError,
)

from app.outbox.publishing.errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishPhase,
    PublishUnexpectedError,
)
from app.outbox.sqs.client import create_sqs_client


def test_client_uses_frozen_credentials_and_one_attempt() -> None:
    session = Mock()
    session.get_credentials.return_value.get_frozen_credentials.return_value = (
        ReadOnlyCredentials("testing", "secret", "token")
    )
    client = create_sqs_client(session=session, region="ap-northeast-1")
    assert client is session.create_client.return_value
    kwargs = session.create_client.call_args.kwargs
    assert kwargs["aws_access_key_id"] == "testing"
    assert kwargs["aws_secret_access_key"] == "secret"
    assert kwargs["aws_session_token"] == "token"
    assert kwargs["region_name"] == "ap-northeast-1"
    assert kwargs["config"].retries == {"mode": "standard", "total_max_attempts": 1}


def test_client_applies_designed_communication_timeouts():
    """設計で定めた接続・応答待ちtimeoutをSQSクライアントへ反映する。"""
    session = Mock()
    session.get_credentials.return_value.get_frozen_credentials.return_value = (
        ReadOnlyCredentials("testing", "secret", "token")
    )
    create_sqs_client(session=session, region="ap-northeast-1")
    config = session.create_client.call_args.kwargs["config"]
    assert config.connect_timeout == 3
    assert config.read_timeout == 5


@pytest.mark.parametrize("during_refresh", [False, True])
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (NoCredentialsError(), PublishConfigurationReason.MISSING_CREDENTIALS),
        (
            PartialCredentialsError(provider="test", cred_var="secret"),
            PublishConfigurationReason.INCOMPLETE_CREDENTIALS,
        ),
        (
            CredentialRetrievalError(provider="test", error_msg="private"),
            PublishConfigurationReason.CREDENTIALS_RETRIEVAL_FAILED,
        ),
        (
            ConnectTimeoutError(endpoint_url="https://credentials.example"),
            PublishConfigurationReason.CREDENTIALS_RETRIEVAL_FAILED,
        ),
        (
            ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole"),
            PublishConfigurationReason.CREDENTIALS_RETRIEVAL_FAILED,
        ),
    ],
)
def test_credentials_failures_never_become_sqs_failures(
    exc, reason, during_refresh
) -> None:
    session = Mock()
    operation = (
        session.get_credentials.return_value.get_frozen_credentials
        if during_refresh
        else session.get_credentials
    )
    operation.side_effect = exc
    with pytest.raises(PublishConfigurationError) as caught:
        create_sqs_client(session=session, region="ap-northeast-1")
    assert caught.value.reason is reason
    assert caught.value.__cause__ is exc
    session.create_client.assert_not_called()


def test_missing_credentials_do_not_fall_back_to_another_provider() -> None:
    session = Mock()
    session.get_credentials.return_value = None
    with pytest.raises(PublishConfigurationError) as caught:
        create_sqs_client(session=session, region="ap-northeast-1")
    assert caught.value.reason is PublishConfigurationReason.MISSING_CREDENTIALS
    session.create_client.assert_not_called()


def test_missing_region_is_rejected_before_credentials_lookup() -> None:
    session = Mock()
    with pytest.raises(PublishConfigurationError) as caught:
        create_sqs_client(session=session, region="")
    assert caught.value.reason is PublishConfigurationReason.MISSING_REGION
    session.get_credentials.assert_not_called()


@pytest.mark.parametrize("operation", ["get_credentials", "create_client"])
def test_non_sdk_failure_retains_phase(operation: str) -> None:
    session = Mock()
    getattr(session, operation).side_effect = RuntimeError("private")
    with pytest.raises(PublishUnexpectedError) as caught:
        create_sqs_client(session=session, region="ap-northeast-1")
    assert caught.value.phase is (
        PublishPhase.RESOLVE_CREDENTIALS
        if operation == "get_credentials"
        else PublishPhase.INITIALIZE
    )
    assert caught.value.original_exception_type == "builtins.RuntimeError"


def test_sdk_region_failure_at_initialization_is_configuration_error() -> None:
    session = Mock()
    session.create_client.side_effect = NoRegionError()
    with pytest.raises(PublishConfigurationError) as caught:
        create_sqs_client(session=session, region="ap-northeast-1")
    assert caught.value.reason is PublishConfigurationReason.MISSING_REGION


@pytest.mark.parametrize("exception_type", [ConnectTimeoutError, ReadTimeoutError])
def test_real_sdk_does_not_retry_sqs_transport(monkeypatch, exception_type) -> None:
    session = botocore.session.get_session()
    session.set_credentials("testing", "testing", "testing")
    client = create_sqs_client(session=session, region="ap-northeast-1")
    send = Mock(side_effect=exception_type(endpoint_url="https://sqs.example"))
    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    try:
        with pytest.raises(exception_type):
            client.send_message_batch(
                QueueUrl="https://sqs.ap-northeast-1.amazonaws.com/123456789012/test",
                Entries=[{"Id": "event", "MessageBody": "body"}],
            )
        assert send.call_count == 1
    finally:
        client.close()


@pytest.mark.parametrize("operation", ["get_credentials", "create_client"])
def test_classifier_failure_during_client_creation_retains_original(
    operation, monkeypatch
):
    """生成境界の分類失敗でも元のSDK障害を失わない。"""
    original = NoRegionError()
    session = Mock()
    getattr(session, operation).side_effect = original
    monkeypatch.setattr(
        "app.outbox.sqs.error_mapping._configuration_error_from_sdk_exception",
        Mock(side_effect=ValueError("private")),
    )
    with pytest.raises(PublishUnexpectedError) as caught:
        create_sqs_client(session=session, region="ap-northeast-1")
    assert caught.value.phase is PublishPhase.CLASSIFY_FAILURE
    assert caught.value.__cause__ is original
    assert caught.value.original_exception_type == "botocore.exceptions.NoRegionError"
    assert caught.value.classification_exception_type == "builtins.ValueError"
