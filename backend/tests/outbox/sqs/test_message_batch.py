"""入力保証の継承と送信本文のサイズ境界を検証する。"""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.outbox.publishing.publisher import EventEnvelope
from app.outbox.sqs.event_batch import EventBatch
from app.outbox.sqs.message_batch import SqsMessageBatch


def envelope(index=1):
    return EventEnvelope(
        event_id=UUID(int=index),
        event_type="article.assessed_in_scope",
        schema_version=1,
        occurred_at=datetime(2026, 9, 7, tzinfo=UTC),
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )


@pytest.mark.parametrize("count", [1, 10])
def test_batch_preserves_input_order_and_contents(count):
    events = EventBatch([envelope(count - i) for i in range(count)])
    batch = SqsMessageBatch(events)
    assert [message.event_id for message in batch.messages] == [
        event.event_id for event in events.envelopes
    ]
    assert all(
        json.loads(m.body)["payload"] == events.envelopes[0].payload
        for m in batch.messages
    )
    assert batch.failures == ()


@pytest.mark.parametrize("count", [0, 11])
def test_input_batch_rejects_invalid_count(count):
    with pytest.raises(ValueError):
        EventBatch([envelope(i) for i in range(count)])


@pytest.mark.parametrize("events", [None, "private", b"private", (object(),)])
def test_input_batch_rejects_invalid_types(events):
    with pytest.raises(TypeError):
        EventBatch(events)


def test_input_snapshot_prevents_list_mutation():
    source = [envelope()]
    events = EventBatch(source)
    source.append(envelope())
    assert events.envelopes == (envelope(),)
    assert len(SqsMessageBatch(events).messages) == 1
    with pytest.raises(FrozenInstanceError):
        events.envelopes = ()


def test_duplicate_ids_are_rejected_before_event_preparation():
    with pytest.raises(ValueError, match="unique"):
        EventBatch([envelope(), replace(envelope(), event_type="private")])


@pytest.mark.parametrize("events", [None, [], (envelope(),)])
def test_message_batch_requires_validated_input(events):
    with pytest.raises(TypeError):
        SqsMessageBatch(events)


def test_arbitrary_messages_cannot_bypass_input_contract():
    with pytest.raises(TypeError):
        SqsMessageBatch(messages=())


def test_invalid_events_leave_ordered_subset_and_individual_failures():
    events = EventBatch(
        [
            envelope(3),
            replace(envelope(2), event_type="unsupported"),
            envelope(1),
        ]
    )
    batch = SqsMessageBatch(events)
    assert [m.event_id for m in batch.messages] == [UUID(int=3), UUID(int=1)]
    assert [f.event_id for f in batch.failures] == [UUID(int=2)]


def test_all_invalid_events_only_produce_failures():
    batch = SqsMessageBatch(EventBatch([replace(envelope(), event_type="unsupported")]))
    assert batch.messages == ()
    assert len(batch.failures) == 1


@pytest.mark.parametrize("extra", [0, 1])
def test_combined_serialized_size_boundary(monkeypatch, extra):
    events = EventBatch([envelope(1), envelope(2)])
    prepared = SqsMessageBatch(events)
    size = sum(len(m.body.encode("utf-8")) for m in prepared.messages)
    monkeypatch.setattr("app.outbox.sqs.message_batch.MAX_MESSAGE_BYTES", size - extra)
    if extra:
        with pytest.raises(ValueError, match="size limit"):
            SqsMessageBatch(events)
    else:
        assert SqsMessageBatch(events).messages == prepared.messages


def test_batch_cannot_be_modified():
    batch = SqsMessageBatch(EventBatch([envelope()]))
    with pytest.raises(FrozenInstanceError):
        batch.messages = ()
    with pytest.raises(FrozenInstanceError):
        batch.failures = ()
    with pytest.raises(TypeError):
        batch.messages[0] = batch.messages[0]
    with pytest.raises(FrozenInstanceError):
        batch.messages[0].body = "changed"
    with pytest.raises(TypeError):
        replace(batch, events=EventBatch([envelope()]), messages=())


def test_batch_display_does_not_expose_body_or_checksum():
    batch = SqsMessageBatch(EventBatch([envelope()]))
    for display in (str(batch), repr(batch)):
        assert batch.messages[0].body not in display
        assert batch.messages[0].body_md5 not in display
