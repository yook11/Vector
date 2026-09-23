"""専用ロールで、1回のRelay実行が残す配送結果を確認する。"""

import asyncio
from contextlib import asynccontextmanager
from hashlib import md5
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app.analysis.assessment.events import ArticleAssessedInScopeEvent
from app.db.errors import DatabaseError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.lambda_handlers.sqs.records import SqsRecord
from app.models.outbox_event import OutboxEvent
from app.outbox.publishing.errors import PublishError, PublishTransportError
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    EventEnvelope,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.sqs.failure_handler import SqsPublishFailureHandler
from local_tests.outbox_relay.support import (
    ClaimHold,
    configuration_error,
    make_relay,
    read_saved,
    seed_event,
    seed_events,
)
from tests.outbox.helpers import PAST, read_event
from tests.outbox.routing_support import make_embedding_publisher

pytestmark = pytest.mark.asyncio


async def test_only_retryable_event_is_published_when_processed_with_exhausted(
    owner_sessions, relay_sessions
):
    """試行回数が残る行と上限到達の行を同時に処理したとき、publishedになるのは残る行だけ。"""
    exhausted = await seed_event(owner_sessions, attempt_count=5)
    retryable = await seed_event(owner_sessions)
    publisher = Mock(
        publish_batch=Mock(
            return_value=BatchPublishResult((PublishSucceeded(retryable.event_id),))
        )
    )

    await make_relay(relay_sessions, publisher).run_once()

    publisher.publish_batch.assert_called_once()
    sent_ids = [
        envelope.event_id for envelope in publisher.publish_batch.call_args.args[0]
    ]
    assert sent_ids == [retryable.event_id]
    saved_retryable = await read_saved(owner_sessions, retryable)
    assert saved_retryable["published_at"] is not None
    assert saved_retryable == {
        **retryable.before,
        "published_at": saved_retryable["published_at"],
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }
    assert (await read_saved(owner_sessions, exhausted))["published_at"] is None


async def test_mixed_results_preserve_success_retry_and_stop(
    owner_sessions, relay_sessions
):
    """同じバッチの成功・再試行・停止をそれぞれ対応するイベントに保存する。"""
    successful, retryable, permanent = await seed_events(owner_sessions, count=3)
    timeout = PublishTransportError(
        failure=HttpTransportFailure(
            HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
        )
    )
    publisher = Mock(
        publish_batch=Mock(
            return_value=BatchPublishResult(
                (
                    PublishSucceeded(successful.event_id),
                    PublishFailed(retryable.event_id, timeout),
                    PublishFailed(permanent.event_id, configuration_error()),
                )
            )
        )
    )

    await make_relay(relay_sessions, publisher).run_once()

    saved_success = await read_saved(owner_sessions, successful)
    assert saved_success["published_at"] is not None
    assert saved_success == {
        **successful.before,
        "published_at": saved_success["published_at"],
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }
    saved_retry = await read_saved(owner_sessions, retryable)
    assert saved_retry["next_attempt_at"] > saved_success["published_at"]
    assert saved_retry == {
        **retryable.before,
        "next_attempt_at": saved_retry["next_attempt_at"],
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }
    saved_stop = await read_saved(owner_sessions, permanent)
    assert saved_stop["delivery_stopped_at"] is not None
    assert saved_stop == {
        **permanent.before,
        "delivery_stopped_at": saved_stop["delivery_stopped_at"],
        "delivery_stop_reason": "non_retryable_failure",
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }
    publisher.publish_batch.assert_called_once()


async def test_empty_claim_does_not_send(relay_sessions):
    """確保対象がなければpublisherを呼ばない。"""
    publisher = Mock()
    await make_relay(relay_sessions, publisher).run_once()
    publisher.publish_batch.assert_not_called()


async def test_leased_event_is_not_resent_by_another_relay(
    owner_sessions, relay_sessions
):
    """先行が確保したイベントを、結果確定前の後続Relayは送らない。"""
    event = await seed_event(owner_sessions)
    hold = ClaimHold()
    finished_stop = False

    @asynccontextmanager
    async def sessions():
        nonlocal finished_stop
        async with relay_sessions() as session:
            yield session
        if not finished_stop:
            finished_stop = True
            return
        if not hold.claimed.is_set():
            hold.claimed.set()
            await asyncio.wait_for(hold.allow.wait(), 10)

    first_publisher = Mock(
        publish_batch=Mock(
            return_value=BatchPublishResult((PublishSucceeded(event.event_id),))
        )
    )
    second_publisher = Mock()
    first_run = asyncio.create_task(make_relay(sessions, first_publisher).run_once())
    try:
        await hold.wait_claimed()
        await make_relay(sessions, second_publisher).run_once()
        second_publisher.publish_batch.assert_not_called()
    finally:
        hold.release()
        await first_run

    first_publisher.publish_batch.assert_called_once()
    saved = await read_saved(owner_sessions, event)
    assert saved["published_at"] is not None
    assert saved == {
        **event.before,
        "published_at": saved["published_at"],
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }


@pytest.mark.parametrize(
    "first_result",
    [
        pytest.param("success", id="success_skipped"),
        pytest.param("failure", id="failure_skipped"),
    ],
)
async def test_skipped_update_does_not_block_later_results(
    owner_sessions, relay_sessions, first_result
):
    """先頭の結果が更新なしでも、後続の成功は記録する。"""
    first, second = await seed_events(owner_sessions, count=2)
    steps = iter(("stop", "claim", "first_result", "later"))

    @asynccontextmanager
    async def sessions():
        step = next(steps)
        if step == "first_result":
            async with owner_sessions() as session:
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.event_id == first.event_id)
                    .values(leased_until=PAST)
                )
                await session.commit()
        async with relay_sessions() as session:
            yield session

    first_publish = (
        PublishFailed(first.event_id, configuration_error())
        if first_result == "failure"
        else PublishSucceeded(first.event_id)
    )
    publisher = Mock(
        publish_batch=Mock(
            return_value=BatchPublishResult(
                (first_publish, PublishSucceeded(second.event_id))
            )
        )
    )
    await make_relay(sessions, publisher).run_once()
    saved_first = await read_saved(owner_sessions, first)
    assert saved_first["lease_token"] is not None
    assert saved_first == {
        **first.before,
        "attempt_count": 1,
        "lease_token": saved_first["lease_token"],
        "leased_until": PAST,
    }
    saved_second = await read_saved(owner_sessions, second)
    assert saved_second["published_at"] is not None
    assert saved_second == {
        **second.before,
        "published_at": saved_second["published_at"],
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }


async def test_claim_fault_keeps_completed_stop_and_does_not_send(
    owner_sessions, relay_sessions
):
    """確保中のDB障害では、確定済みの停止を残し送信しない。"""
    exhausted = await seed_event(owner_sessions, attempt_count=5)
    ready = await seed_event(owner_sessions)
    fault = SQLAlchemyError("sensitive database diagnostic")
    steps = iter(("stop", "claim"))

    def fail(*_args, **_kwargs):
        raise fault

    @asynccontextmanager
    async def sessions():
        step = next(steps)
        async with relay_sessions() as session:
            if step == "claim":
                sqlalchemy_event.listen(session.sync_session, "before_commit", fail)
            yield session

    publisher = Mock()
    with pytest.raises(DatabaseError) as caught:
        await make_relay(sessions, publisher).run_once()
    assert caught.value.__cause__ is fault
    assert not isinstance(caught.value, PublishError)
    publisher.publish_batch.assert_not_called()
    saved_exhausted = await read_saved(owner_sessions, exhausted)
    assert saved_exhausted["delivery_stopped_at"] is not None
    assert saved_exhausted == {
        **exhausted.before,
        "delivery_stopped_at": saved_exhausted["delivery_stopped_at"],
        "delivery_stop_reason": "retry_exhausted",
        "lease_token": None,
        "leased_until": None,
    }
    assert await read_saved(owner_sessions, ready) == ready.before


async def test_result_fault_keeps_earlier_publish_and_does_not_resend(
    owner_sessions, relay_sessions
):
    """後続結果のDB障害では、先に確定した成功を残し再送しない。"""
    first, second, third = await seed_events(owner_sessions, count=3)
    fault = SQLAlchemyError("sensitive database diagnostic")
    steps = iter(("stop", "claim", "first_result", "second_result"))

    def fail(*_args, **_kwargs):
        raise fault

    @asynccontextmanager
    async def sessions():
        step = next(steps)
        async with relay_sessions() as session:
            if step == "second_result":
                sqlalchemy_event.listen(session.sync_session, "before_commit", fail)
            yield session

    publisher = Mock(
        publish_batch=Mock(
            return_value=BatchPublishResult(
                tuple(
                    PublishSucceeded(event.event_id) for event in (first, second, third)
                )
            )
        )
    )
    with pytest.raises(DatabaseError) as caught:
        await make_relay(sessions, publisher).run_once()
    assert caught.value.__cause__ is fault
    publisher.publish_batch.assert_called_once()
    saved_first = await read_saved(owner_sessions, first)
    assert saved_first["published_at"] is not None
    assert saved_first == {
        **first.before,
        "published_at": saved_first["published_at"],
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }
    saved_second = await read_saved(owner_sessions, second)
    assert saved_second["lease_token"] is not None
    assert saved_second == {
        **second.before,
        "attempt_count": 1,
        "lease_token": saved_second["lease_token"],
        "leased_until": saved_second["leased_until"],
    }
    saved_third = await read_saved(owner_sessions, third)
    assert saved_third["lease_token"] is not None
    assert saved_third == {
        **third.before,
        "attempt_count": 1,
        "lease_token": saved_third["lease_token"],
        "leased_until": saved_third["leased_until"],
    }


@pytest.mark.parametrize(
    "case",
    ["batch_type", "missing", "extra", "wrong_id", "reversed", "duplicate"],
)
async def test_invalid_publisher_result_keeps_all_events_claimed(
    owner_sessions, relay_sessions, case
):
    """結果の対応が崩れたら例外にし、どのイベントも配送結果を書かない。"""
    first, second = await seed_events(owner_sessions, count=2)

    def send(envelopes):
        results = tuple(PublishSucceeded(envelope.event_id) for envelope in envelopes)
        return {
            "batch_type": None,
            "missing": BatchPublishResult(results[:1]),
            "extra": BatchPublishResult(results + (PublishSucceeded(uuid4()),)),
            "wrong_id": BatchPublishResult((results[0], PublishSucceeded(uuid4()))),
            "reversed": BatchPublishResult(results[::-1]),
            "duplicate": BatchPublishResult((results[0], results[0])),
        }[case]

    publisher = Mock(publish_batch=Mock(side_effect=send))
    with pytest.raises((TypeError, ValueError)) as caught:
        await make_relay(relay_sessions, publisher).run_once()
    assert "private body" not in str(caught.value)
    publisher.publish_batch.assert_called_once()
    for event in (first, second):
        saved = await read_saved(owner_sessions, event)
        assert saved["lease_token"] is not None
        assert saved == {
            **event.before,
            "attempt_count": 1,
            "lease_token": saved["lease_token"],
            "leased_until": saved["leased_until"],
        }


@pytest.mark.parametrize("stage", ["envelope", "publisher"])
@pytest.mark.parametrize(
    "error",
    [ValueError("batch exceeds limit"), RuntimeError("failure"), KeyboardInterrupt()],
)
async def test_call_failure_keeps_claimed_event_and_propagates(
    owner_sessions, relay_sessions, monkeypatch, stage, error
):
    """送信準備や呼び出しの例外を配送失敗へ変換せず、確保を残す。"""
    event = await seed_event(owner_sessions)
    publisher = Mock(publish_batch=Mock(side_effect=error))
    if stage == "envelope":
        monkeypatch.setattr(EventEnvelope, "from_claimed", Mock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        await make_relay(relay_sessions, publisher).run_once()
    assert caught.value is error
    assert publisher.publish_batch.call_count == (1 if stage == "publisher" else 0)
    saved = await read_saved(owner_sessions, event)
    assert saved["lease_token"] is not None
    assert saved == {
        **event.before,
        "attempt_count": 1,
        "lease_token": saved["lease_token"],
        "leased_until": saved["leased_until"],
    }


@pytest.mark.parametrize(
    "invalid", [{"schema_version": 2}, {"payload": {"curation_id": 1}}]
)
async def test_invalid_event_is_stopped_and_valid_event_is_delivered(
    owner_sessions, relay_sessions, invalid
):
    """本文不正は停止し、契約を満たすイベントは受信側でも復元できる。"""
    bad, good = await seed_events(owner_sessions, count=2)
    async with owner_sessions() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.event_id == bad.event_id)
            .values(**invalid)
        )
        await session.commit()
        bad_before = await read_event(session, bad.event_id)

    def send(**kwargs):
        entries = kwargs["Entries"]
        assert len(entries) == 1
        event = ArticleAssessedInScopeEvent.from_input(
            SqsRecord(message_id="id", body=entries[0]["MessageBody"]).parse_json()
        )
        assert event.event_id == good.event_id
        assert event.payload.model_dump() == good.before["payload"]
        return {
            "Successful": [
                {
                    "Id": str(good.event_id),
                    "MessageId": "test-message",
                    "MD5OfMessageBody": md5(
                        entries[0]["MessageBody"].encode(), usedforsecurity=False
                    ).hexdigest(),
                }
            ]
        }

    client = Mock()
    client.send_message_batch.side_effect = send
    publisher = make_embedding_publisher(
        failure_handler=SqsPublishFailureHandler(),
        embedding_queue_url="https://sqs.invalid/embedding",
        client_factory=lambda: client,
    )
    await make_relay(relay_sessions, publisher).run_once()
    saved_bad = await read_saved(owner_sessions, bad)
    assert saved_bad["delivery_stopped_at"] is not None
    assert saved_bad == {
        **bad_before,
        "delivery_stopped_at": saved_bad["delivery_stopped_at"],
        "delivery_stop_reason": "non_retryable_failure",
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }
    saved_good = await read_saved(owner_sessions, good)
    assert saved_good["published_at"] is not None
    assert saved_good == {
        **good.before,
        "published_at": saved_good["published_at"],
        "attempt_count": 1,
        "lease_token": None,
        "leased_until": None,
    }
    client.send_message_batch.assert_called_once()
    await make_relay(relay_sessions, publisher).run_once()
    client.send_message_batch.assert_called_once()
    assert await read_saved(owner_sessions, bad) == saved_bad
    assert await read_saved(owner_sessions, good) == saved_good
