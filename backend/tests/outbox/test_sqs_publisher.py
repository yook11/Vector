from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import botocore.session
import pytest
from botocore.stub import Stubber

from app.outbox.publish_errors import (
    PublishEventInvalidError,
    PublishServiceError,
    PublishServiceReason,
)
from app.outbox.publisher import EventEnvelope
from app.outbox.repository import ClaimedOutboxEvent
from app.outbox.sqs_publisher import SqsEventPublisher

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/article-embedding"


@pytest.fixture
def claimed() -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        event_id=UUID("b8969c8e-5c43-4b5e-9867-b20768551666"),
        event_type="article.assessed_in_scope",
        schema_version=1,
        occurred_at=datetime(2026, 9, 7, 3, tzinfo=UTC),
        payload={"curation_id": 123, "analyzed_article_id": 456},
        attempt_count=2,
        lease_token=UUID("b8969c8e-5c43-4b5e-9867-b20768551667"),
        leased_until=datetime(2026, 9, 7, 4, tzinfo=UTC),
    )


@pytest.fixture
def publisher() -> Iterator[tuple[SqsEventPublisher, Stubber]]:
    client = botocore.session.get_session().create_client(
        "sqs",
        region_name="ap-northeast-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    try:
        with Stubber(client) as stubber:
            yield (
                SqsEventPublisher(
                    client_factory=lambda: client, embedding_queue_url=QUEUE_URL
                ),
                stubber,
            )
            stubber.assert_no_pending_responses()
    finally:
        client.close()


def _expected_params(
    claimed: ClaimedOutboxEvent, occurred_at: str = "2026-09-07T03:00:00Z"
) -> dict[str, str]:
    return {
        "QueueUrl": QUEUE_URL,
        "MessageBody": json.dumps(
            {
                "event_id": str(claimed.event_id),
                "event_type": claimed.event_type,
                "schema_version": claimed.schema_version,
                "occurred_at": occurred_at,
                "payload": claimed.payload,
            }
        ),
    }


def test_publish_preserves_event_body_on_retry(
    claimed: ClaimedOutboxEvent, publisher: tuple[SqsEventPublisher, Stubber]
) -> None:
    """配信管理情報を除く5項目だけを送り、再送でも内容を維持する。"""
    sender, stubber = publisher
    envelope = EventEnvelope.from_claimed(claimed)
    for _ in range(2):
        stubber.add_response("send_message", {}, _expected_params(claimed))
        assert sender.publish(envelope) is None
    assert envelope.payload == claimed.payload


def test_publish_converts_offset_to_utc_and_preserves_microseconds(
    claimed: ClaimedOutboxEvent, publisher: tuple[SqsEventPublisher, Stubber]
) -> None:
    """UTCへの変換で日時の意味と小数秒を維持する。"""
    sender, stubber = publisher
    event = replace(
        claimed,
        occurred_at=datetime(
            2026, 9, 7, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
        ),
    )
    stubber.add_response(
        "send_message", {}, _expected_params(event, "2026-09-07T03:00:00.123456Z")
    )
    sender.publish(EventEnvelope.from_claimed(event))


def test_publish_rejects_unsupported_event_without_sending(
    claimed: ClaimedOutboxEvent, publisher: tuple[SqsEventPublisher, Stubber]
) -> None:
    """送信先のないイベントをembeddingキューへ流さない。"""
    sender, _ = publisher
    envelope = EventEnvelope.from_claimed(
        replace(claimed, event_type="article.acquired")
    )
    with pytest.raises(PublishEventInvalidError):
        sender.publish(envelope)


def test_publish_rejects_naive_datetime_without_sending(
    claimed: ClaimedOutboxEvent, publisher: tuple[SqsEventPublisher, Stubber]
) -> None:
    """タイムゾーンのない日時を暗黙に補って送信しない。"""
    sender, _ = publisher
    event = replace(claimed, occurred_at=datetime(2026, 9, 7, 3))
    with pytest.raises(PublishEventInvalidError):
        sender.publish(EventEnvelope.from_claimed(event))


def test_publish_translates_sdk_failure(
    claimed: ClaimedOutboxEvent, publisher: tuple[SqsEventPublisher, Stubber]
) -> None:
    """SDKの応答を共通理由へ変換する。"""
    sender, stubber = publisher
    stubber.add_client_error(
        "send_message",
        service_error_code="AccessDenied",
        service_message="Denied",
        http_status_code=403,
        expected_params=_expected_params(claimed),
    )
    with pytest.raises(PublishServiceError) as error:
        sender.publish(EventEnvelope.from_claimed(claimed))
    assert error.value.service_error_code == "AccessDenied"
    assert error.value.reason is PublishServiceReason.ACCESS_DENIED


@pytest.mark.parametrize("payload", [{"bad": object()}, {"bad": float("nan")}])
def test_serialization_failure_precedes_client_creation(claimed, payload) -> None:
    from unittest.mock import Mock

    from app.outbox.publish_errors import PublishEventInvalidReason

    factory = Mock()
    sender = SqsEventPublisher(client_factory=factory, embedding_queue_url=QUEUE_URL)
    with pytest.raises(PublishEventInvalidError) as caught:
        sender.publish(replace(EventEnvelope.from_claimed(claimed), payload=payload))
    assert caught.value.reason is PublishEventInvalidReason.SERIALIZATION_FAILED
    factory.assert_not_called()


@pytest.mark.parametrize("close_fails", [False, True])
def test_unexpected_send_failure_retains_original_and_closes_client(
    claimed, close_fails
) -> None:
    from unittest.mock import Mock

    from app.outbox.publish_errors import PublishPhase, PublishUnexpectedError

    client = Mock()
    exc = RuntimeError("private")
    client.send_message.side_effect = exc
    if close_fails:
        client.close.side_effect = ValueError("cleanup")
    sender = SqsEventPublisher(
        client_factory=lambda: client, embedding_queue_url=QUEUE_URL
    )
    with pytest.raises(PublishUnexpectedError) as caught:
        sender.publish(EventEnvelope.from_claimed(claimed))
    assert caught.value.phase is PublishPhase.SEND
    assert caught.value.original_exception_type == "builtins.RuntimeError"
    assert caught.value.__cause__ is exc
    client.close.assert_called_once()


def test_cleanup_failure_after_success_has_distinct_phase(claimed) -> None:
    from unittest.mock import Mock

    from app.outbox.publish_errors import PublishPhase, PublishUnexpectedError

    client = Mock()
    client.close.side_effect = RuntimeError("private")
    sender = SqsEventPublisher(
        client_factory=lambda: client, embedding_queue_url=QUEUE_URL
    )
    with pytest.raises(PublishUnexpectedError) as caught:
        sender.publish(EventEnvelope.from_claimed(claimed))
    assert caught.value.phase is PublishPhase.CLEANUP
    client.send_message.assert_called_once()


def test_classifier_failure_preserves_both_exception_types(
    claimed, monkeypatch
) -> None:
    from unittest.mock import Mock

    from app.outbox.publish_errors import PublishPhase, PublishUnexpectedError

    original = RuntimeError("original private")
    client = Mock()
    client.send_message.side_effect = original
    monkeypatch.setattr(
        "app.outbox.sqs_publisher.publish_error_from_sqs_exception",
        Mock(side_effect=ValueError("classifier private")),
    )
    sender = SqsEventPublisher(
        client_factory=lambda: client, embedding_queue_url=QUEUE_URL
    )
    with pytest.raises(PublishUnexpectedError) as caught:
        sender.publish(EventEnvelope.from_claimed(claimed))
    assert caught.value.phase is PublishPhase.CLASSIFY_FAILURE
    assert caught.value.original_exception_type == "builtins.RuntimeError"
    assert caught.value.classification_exception_type == "builtins.ValueError"
    assert caught.value.__cause__ is original


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
def test_process_exit_is_not_wrapped_and_cleanup_cannot_replace_it(
    claimed, exc
) -> None:
    from unittest.mock import Mock

    client = Mock()
    client.send_message.side_effect = exc
    client.close.side_effect = ValueError("cleanup")
    sender = SqsEventPublisher(
        client_factory=lambda: client, embedding_queue_url=QUEUE_URL
    )
    with pytest.raises(type(exc)) as caught:
        sender.publish(EventEnvelope.from_claimed(claimed))
    assert caught.value is exc
    client.close.assert_called_once()


def test_existing_publish_error_is_not_wrapped(claimed) -> None:
    from unittest.mock import Mock

    from app.outbox.publish_errors import (
        PublishConfigurationError,
        PublishConfigurationReason,
    )

    exc = PublishConfigurationError(
        reason=PublishConfigurationReason.MISSING_CREDENTIALS
    )
    sender = SqsEventPublisher(
        client_factory=Mock(side_effect=exc), embedding_queue_url=QUEUE_URL
    )
    with pytest.raises(PublishConfigurationError) as caught:
        sender.publish(EventEnvelope.from_claimed(claimed))
    assert caught.value is exc


def test_each_publish_creates_and_closes_its_own_client(claimed) -> None:
    from unittest.mock import Mock

    clients = [Mock(), Mock()]
    factory = Mock(side_effect=clients)
    sender = SqsEventPublisher(client_factory=factory, embedding_queue_url=QUEUE_URL)
    envelope = EventEnvelope.from_claimed(claimed)
    sender.publish(envelope)
    sender.publish(envelope)
    assert factory.call_count == 2
    assert clients[0].send_message.call_args == clients[1].send_message.call_args
    for client in clients:
        client.close.assert_called_once()


def test_session_entrypoint_resolves_credentials_again_for_each_publish(
    claimed,
    monkeypatch,
) -> None:
    """送信ごとに資格情報を確定し、各試行へ新しいクライアントを渡す。"""
    from unittest.mock import Mock

    from botocore.credentials import ReadOnlyCredentials

    session = Mock()
    clients = [Mock(), Mock()]
    session.create_client.side_effect = clients
    freeze = session.get_credentials.return_value.get_frozen_credentials
    freeze.side_effect = [
        ReadOnlyCredentials("first", "secret", "token1"),
        ReadOnlyCredentials("second", "secret", "token2"),
    ]
    sender = SqsEventPublisher.from_session(
        session=session, region="ap-northeast-1", embedding_queue_url=QUEUE_URL
    )
    sender.publish(EventEnvelope.from_claimed(claimed))
    sender.publish(EventEnvelope.from_claimed(claimed))
    assert freeze.call_count == 2
    assert [
        call.kwargs["aws_access_key_id"]
        for call in session.create_client.call_args_list
    ] == ["first", "second"]
    for client in clients:
        client.send_message.assert_called_once()
        client.close.assert_called_once()
