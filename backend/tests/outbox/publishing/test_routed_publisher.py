"""バッチの受付結果と、個別失敗・全体失敗の境界を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from hashlib import md5
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.outbox.delivery.repository import ClaimedOutboxEvent
from app.outbox.publishing.errors import (
    PublishEventInvalidReason,
)
from app.outbox.publishing.publisher import (
    EventEnvelope,
)
from app.outbox.sqs.failure_handler import SqsPublishFailureHandler
from app.outbox.sqs.message import SqsMessage
from tests.outbox.routing_support import (
    make_embedding_publisher,
)

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/article-embedding"


LIMIT = 512


@pytest.fixture
def envelope():
    return EventEnvelope(
        event_id=UUID(int=1),
        event_type="article.assessed_in_scope",
        schema_version=1,
        occurred_at=datetime(2026, 9, 7, 3, tzinfo=UTC),
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )


def _success(envelope):
    return {
        "Id": str(envelope.event_id),
        "MessageId": "sqs-id",
        "MD5OfMessageBody": md5(
            _body(envelope).encode("utf-8"), usedforsecurity=False
        ).hexdigest(),
    }


def _body(envelope):
    return json.dumps(
        {
            "event_id": str(envelope.event_id),
            "event_type": envelope.event_type,
            "schema_version": envelope.schema_version,
            "occurred_at": envelope.occurred_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "payload": envelope.payload,
        },
        allow_nan=False,
    )


def _sender(*, response=None, error=None, close_error=None):
    client = Mock()
    client.send_message_batch.return_value = response
    client.send_message_batch.side_effect = error
    client.close.side_effect = close_error
    factory = Mock(return_value=client)
    return (
        make_embedding_publisher(
            failure_handler=SqsPublishFailureHandler(),
            client_factory=factory,
            embedding_queue_url=QUEUE_URL,
        ),
        client,
        factory,
    )


@pytest.mark.parametrize(
    "case,exception",
    [
        ("empty", ValueError),
        ("eleven", ValueError),
        ("duplicate", ValueError),
        ("none", TypeError),
        ("string", TypeError),
        ("iterator", TypeError),
        ("element", TypeError),
        ("event_id", TypeError),
        ("event_type", TypeError),
        ("schema_version", TypeError),
        ("occurred_at", TypeError),
        ("payload", TypeError),
    ],
)
def test_call_contract_violations_precede_client_creation(envelope, case, exception):
    """入力契約違反を本文生成・SDK初期化の前に拒否する。"""
    inputs = {
        "empty": [],
        "eleven": [replace(envelope, event_id=UUID(int=i)) for i in range(11)],
        "duplicate": [envelope, envelope],
        "none": None,
        "string": "private",
        "iterator": iter([envelope]),
        "element": [object()],
        "event_id": [replace(envelope, event_id="private")],
        "event_type": [replace(envelope, event_type=1)],
        "schema_version": [replace(envelope, schema_version=True)],
        "occurred_at": [replace(envelope, occurred_at="private")],
        "payload": [replace(envelope, payload=[])],
    }
    sender, _, factory = _sender()
    with pytest.raises(exception):
        sender.publish_batch(inputs[case])
    factory.assert_not_called()


def test_input_count_is_checked_before_excluding_invalid_events(envelope, monkeypatch):
    """準備失敗を除けば10件になる入力でも、11件の呼び出し自体を拒否する。"""
    events = [replace(envelope, event_id=UUID(int=index)) for index in range(11)]
    events[0] = replace(events[0], event_type="unsupported")
    prepare = Mock(side_effect=AssertionError("preparation must not run"))
    monkeypatch.setattr(SqsMessage, "from_message", prepare)
    sender, _, factory = _sender()
    with pytest.raises(ValueError):
        sender.publish_batch(events)
    prepare.assert_not_called()
    factory.assert_not_called()


def test_all_invalid_events_do_not_send_an_empty_batch(envelope):
    """送信対象がなければAWS送信を呼ばず個別失敗を返す。"""
    sender, client, factory = _sender()
    result = sender.publish_batch([replace(envelope, event_type="unsupported")])
    assert (
        result.results[0].error.reason
        is PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
    )
    client.send_message_batch.assert_not_called()
    factory.assert_not_called()


def test_utc_precision_and_claimed_payload_copy(envelope):
    """時刻の意味と小数秒を保ち、確保情報や本文の参照を送信へ持ち込まない。"""
    claimed = ClaimedOutboxEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        schema_version=1,
        occurred_at=datetime(
            2026, 9, 7, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
        ),
        payload=deepcopy(envelope.payload),
        attempt_count=2,
        lease_token=UUID(int=2),
        leased_until=datetime(2026, 9, 7, 4, tzinfo=UTC),
    )
    event = EventEnvelope.from_claimed(claimed)
    claimed.payload["curation_id"] = 999
    sender, client, _ = _sender(response={"Successful": [_success(event)]})
    sender.publish_batch([event])
    body = json.loads(
        client.send_message_batch.call_args.kwargs["Entries"][0]["MessageBody"]
    )
    assert body == {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "schema_version": 1,
        "occurred_at": "2026-09-07T03:00:00.123456Z",
        "payload": {"curation_id": 123, "analyzed_article_id": 456},
    }


@pytest.mark.parametrize("include_valid", [False, True])
def test_real_embedding_validation_excludes_only_rejected_event(
    envelope, include_valid
):
    """実イベント契約の拒否を個別失敗にし、正常な同居イベントだけ送る。"""
    from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
    from app.outbox.publishing.publisher import BatchPublishResult, PublishSucceeded
    from app.outbox.publishing.route import EventDeliveryRoute
    from app.outbox.publishing.routed_publisher import RoutedEventPublisher

    invalid = replace(envelope, event_type="another.event")
    good = replace(envelope, event_id=UUID(int=2))
    sender = Mock()
    sender.send_batch.return_value = BatchPublishResult(
        (PublishSucceeded(good.event_id),)
    )
    publisher = RoutedEventPublisher(
        EventDeliveryRoute(
            envelope.event_type, QUEUE_URL, build_assessed_in_scope_message
        ),
        sender,
    )
    result = publisher.publish_batch([invalid, good] if include_valid else [invalid])
    assert (
        result.results[0].error.reason
        is PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
    )
    if include_valid:
        sender.send_batch.assert_called_once_with(
            queue_url=QUEUE_URL, messages=(build_assessed_in_scope_message(good),)
        )
        assert result.results[1] == PublishSucceeded(good.event_id)
    else:
        sender.send_batch.assert_not_called()


def test_custom_route_combines_preparation_and_send_results_in_input_order(envelope):
    """工程に依存しない配送定義で、生成失敗・送信失敗・成功を入力順に統合する。"""
    from app.outbox.publishing.errors import PublishEventInvalidError, PublishPhase
    from app.outbox.publishing.publisher import (
        BatchPublishResult,
        PublishFailed,
        PublishSucceeded,
    )
    from app.outbox.publishing.route import EventDeliveryRoute, EventMessage
    from app.outbox.publishing.routed_publisher import RoutedEventPublisher

    events = [
        replace(envelope, event_id=UUID(int=i), event_type="test.event")
        for i in range(1, 4)
    ]
    original = RuntimeError("PRIVATE")
    prepared = [EventMessage(e.event_id, "test body") for e in events[1:]]
    builder = Mock(side_effect=[original, *prepared])
    send_error = PublishEventInvalidError(
        reason=PublishEventInvalidReason.MESSAGE_TOO_LARGE
    )
    outcomes = (
        PublishFailed(events[1].event_id, send_error),
        PublishSucceeded(events[2].event_id),
    )
    sender = Mock()
    sender.send_batch.return_value = BatchPublishResult(outcomes)
    publisher = RoutedEventPublisher(
        EventDeliveryRoute("test.event", "test-queue", builder), sender
    )
    result = publisher.publish_batch(events)
    assert [r.event_id for r in result.results] == [e.event_id for e in events]
    assert result.results[0].error.__cause__ is original
    assert result.results[0].error.phase is PublishPhase.PREPARE_EVENT
    assert result.results[1:] == outcomes
    assert [c.args[0] for c in builder.call_args_list] == events
    sender.send_batch.assert_called_once_with(
        queue_url="test-queue", messages=tuple(prepared)
    )


@pytest.mark.parametrize("case", ["wrong_type", "wrong_id", "wrong_event_type"])
def test_builder_contract_violation_never_reaches_sender(envelope, case):
    """生成成功でも本文型・イベントID・配送種別の不一致を送信前に拒否する。"""
    from app.outbox.publishing.route import EventDeliveryRoute, EventMessage
    from app.outbox.publishing.routed_publisher import RoutedEventPublisher

    message = {
        "wrong_type": object(),
        "wrong_id": EventMessage(UUID(int=2), "body"),
        "wrong_event_type": EventMessage(envelope.event_id, "body"),
    }[case]
    route = EventDeliveryRoute(
        "different" if case == "wrong_event_type" else envelope.event_type,
        QUEUE_URL,
        Mock(return_value=message),
    )
    sender = Mock()
    result = RoutedEventPublisher(route, sender).publish_batch([envelope])
    assert result.results[0].event_id == envelope.event_id
    sender.send_batch.assert_not_called()


@pytest.mark.parametrize("ids", [[], [1, 3], [1, 1], [2, 1]])
def test_sender_result_correspondence_is_required(envelope, ids):
    """送信結果の欠落・未知ID・重複・順序違反を正常な配送結果として返さない。"""
    from app.outbox.publishing.publisher import BatchPublishResult, PublishSucceeded
    from app.outbox.publishing.route import EventDeliveryRoute, EventMessage
    from app.outbox.publishing.routed_publisher import RoutedEventPublisher

    sender = Mock()
    sender.send_batch.return_value = BatchPublishResult(
        tuple(PublishSucceeded(UUID(int=i)) for i in ids)
    )
    route = EventDeliveryRoute(
        envelope.event_type, QUEUE_URL, lambda e: EventMessage(e.event_id, "body")
    )
    with pytest.raises(ValueError):
        RoutedEventPublisher(route, sender).publish_batch(
            [envelope, replace(envelope, event_id=UUID(int=2))]
        )


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("event_type", " ", ValueError),
        ("queue_url", "\t", ValueError),
        ("event_type", None, TypeError),
        ("queue_url", None, TypeError),
        ("build_message", None, TypeError),
    ],
)
def test_route_requires_explicit_nonblank_fields(envelope, field, value, error):
    """配送先・種別・本文生成処理を必須とし、未設定を既定値で補わない。"""
    from app.outbox.publishing.route import EventDeliveryRoute, EventMessage

    args = dict(
        event_type=envelope.event_type,
        queue_url=QUEUE_URL,
        build_message=lambda e: EventMessage(e.event_id, "body"),
    )
    args[field] = value
    with pytest.raises(error):
        EventDeliveryRoute(**args)
