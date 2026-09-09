"""relayの配線と、送信を挟むトランザクション境界を検証する。"""

from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, call
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app.analysis.assessment.events import ArticleAssessedInScope
from app.db.errors import DatabaseError
from app.db.session import caller_managed_session_factory
from app.http.failure import HttpTransportFailure, HttpTransportFailureKind
from app.models.outbox_event import OutboxEvent
from app.outbox import publish_failure_recording
from app.outbox.publish_errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishTransportError,
)
from app.outbox.publish_failure_handler import PublishFailureHandler
from app.outbox.publisher import (
    BatchPublishResult,
    EventEnvelope,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.relay import OutboxRelay
from app.outbox.repository import OutboxDeliveryRepository
from tests.outbox.helpers import PAST, insert_event, read_event

pytestmark = pytest.mark.asyncio
EVENT_TYPE = ArticleAssessedInScope.EVENT_TYPE


def _configuration():
    return PublishConfigurationError(
        reason=PublishConfigurationReason.MISSING_CREDENTIALS
    )


def _transport():
    return PublishTransportError(
        failure=HttpTransportFailure(HttpTransportFailureKind.READ_TIMEOUT, True)
    )


async def _seed(factory, count=1, **fields):
    async with factory() as session:
        ids = [
            await insert_event(
                session,
                next_attempt_at=PAST,
                event_type=fields.get("event_type", EVENT_TYPE),
                occurred_at=PAST + timedelta(seconds=i),
                payload={"curation_id": i + 1, "analyzed_article_id": i + 101},
                attempt_count=fields.get("attempt_count", 0),
            )
            for i in range(count)
        ]
        await session.commit()
        return {event_id: await read_event(session, event_id) for event_id in ids}


async def _read(factory, ids):
    async with factory() as session:
        return {event_id: await read_event(session, event_id) for event_id in ids}


class Sessions:
    """実sessionを使い、処理順の観測と指定位置への障害注入を行う。"""

    def __init__(self, engine):
        self.base = caller_managed_session_factory(engine)
        self.count = 0
        self.active = 0
        self.trace = []
        self.fail_number = None
        self.fail_at = None
        self.error = SQLAlchemyError("sensitive database diagnostic")
        self.before_open = None

    def fail(self, *args, **kwargs):
        raise self.error

    @asynccontextmanager
    async def __call__(self):
        self.count += 1
        number = self.count
        if self.before_open:
            await self.before_open(number)
        try:
            async with self.base() as session:
                sqlalchemy_event.listen(
                    session.sync_session,
                    "after_commit",
                    lambda _: self.trace.append((number, "commit")),
                )
                sqlalchemy_event.listen(
                    session.sync_session,
                    "after_rollback",
                    lambda _: self.trace.append((number, "rollback")),
                )
                if number == self.fail_number:
                    if self.fail_at == "commit":
                        sqlalchemy_event.listen(
                            session.sync_session, "before_commit", self.fail
                        )
                    elif self.fail_at == "update":
                        connection = await session.connection()
                        sqlalchemy_event.listen(
                            connection.sync_connection,
                            "after_cursor_execute",
                            self.fail,
                            once=True,
                        )
                self.active += 1
                self.trace.append((number, "open"))
                try:
                    yield session
                finally:
                    self.active -= 1
        finally:
            self.trace.append((number, "closed"))
        if number == self.fail_number and self.fail_at == "exit":
            raise self.error


@pytest.fixture
async def sessions(session_factory):
    async with session_factory() as session:
        return Sessions(session.bind)


@pytest.fixture
def outputs(monkeypatch):
    stopped = Mock()
    metric = Mock()
    monkeypatch.setattr(publish_failure_recording.logger, "info", stopped)
    monkeypatch.setattr(publish_failure_recording, "emit_metric", metric)
    return stopped, metric


def _relay(sessions, publisher):
    return OutboxRelay(
        sessions, publisher, PublishFailureHandler(sessions, jitter=lambda: 0.5)
    )


async def test_run_once_commits_preparation_before_sending_and_records_success(
    session_factory, sessions
):
    """停止・確保を確定してsessionを閉じてから送信し、成功結果を確定する。"""
    exhausted = await _seed(session_factory, attempt_count=5)
    ready = await _seed(session_factory)
    exhausted_id = next(iter(exhausted))
    event_id = next(iter(ready))

    async def before_open(number):
        if number == 2:
            saved = await _read(session_factory, exhausted)
            assert saved[exhausted_id]["delivery_stopped_at"] is not None
        if number == 3:
            claimed = await _read(session_factory, ready)
            assert claimed[event_id]["lease_token"] is not None
            assert claimed[event_id]["published_at"] is None

    sessions.before_open = before_open

    def send(envelopes):
        assert sessions.active == 0
        assert sessions.trace == [
            (1, "open"),
            (1, "commit"),
            (1, "closed"),
            (2, "open"),
            (2, "commit"),
            (2, "closed"),
        ]
        assert [envelope.event_id for envelope in envelopes] == [event_id]
        return BatchPublishResult((PublishSucceeded(event_id),))

    publisher = Mock(publish_batch=Mock(side_effect=send))
    assert await _relay(sessions, publisher).run_once() is None
    publisher.publish_batch.assert_called_once()
    saved = await _read(session_factory, ready)
    assert saved[event_id]["published_at"] is not None
    assert sessions.trace[-3:] == [(3, "open"), (3, "commit"), (3, "closed")]


async def test_empty_claim_finishes_without_sending(sessions):
    """確保対象がなければpublisherを呼ばず、その実行を終了する。"""
    publisher = Mock()
    assert await _relay(sessions, publisher).run_once() is None
    publisher.publish_batch.assert_not_called()
    assert sessions.count == 2


async def test_preparation_passes_embedding_scope_and_processing_limits(
    sessions, monkeypatch
):
    """対象タイプ・停止100件・確保10件・lease150秒をrepositoryへ渡す。"""
    calls = Mock()
    stop = OutboxDeliveryRepository.stop_deliveries_at_attempt_limit
    claim = OutboxDeliveryRepository.claim_ready_batch

    async def observe_stop(repository, **kwargs):
        calls.stop(**kwargs)
        return await stop(repository, **kwargs)

    async def observe_claim(repository, **kwargs):
        calls.claim(**kwargs)
        return await claim(repository, **kwargs)

    monkeypatch.setattr(
        OutboxDeliveryRepository, "stop_deliveries_at_attempt_limit", observe_stop
    )
    monkeypatch.setattr(OutboxDeliveryRepository, "claim_ready_batch", observe_claim)
    await _relay(sessions, Mock()).run_once()
    assert calls.mock_calls == [
        call.stop(event_type=EVENT_TYPE, limit=100),
        call.claim(
            event_type=EVENT_TYPE, limit=10, lease_duration=timedelta(seconds=150)
        ),
    ]


async def test_mixed_results_route_success_and_failures_without_duplicate_recording(
    session_factory, sessions, outputs, monkeypatch
):
    """各結果を成功記録とhandlerへ振り分け、停止の記録を二重に呼ばない。"""
    before = await _seed(session_factory, count=3)
    success_id, retry_id, stop_id = before
    retry = PublishFailed(retry_id, _transport())
    stop = PublishFailed(stop_id, _configuration())
    publisher = Mock(
        publish_batch=Mock(
            return_value=BatchPublishResult((PublishSucceeded(success_id), retry, stop))
        )
    )
    handler = PublishFailureHandler(sessions, jitter=lambda: 0.5)
    handle = AsyncMock(wraps=handler.handle)
    monkeypatch.setattr(handler, "handle", handle)
    mark_published = OutboxDeliveryRepository.mark_published
    success_calls = Mock()

    async def observe_success(repository, **kwargs):
        success_calls(**kwargs)
        return await mark_published(repository, **kwargs)

    monkeypatch.setattr(OutboxDeliveryRepository, "mark_published", observe_success)
    await OutboxRelay(sessions, publisher, handler).run_once()

    success_calls.assert_called_once()
    assert success_calls.call_args.kwargs["event_id"] == success_id
    assert handle.await_count == 2
    for invocation, failure in zip(handle.await_args_list, (retry, stop), strict=True):
        assert invocation.kwargs["event"].event_id == failure.event_id
        assert invocation.kwargs["failure"] is failure
    outputs[0].assert_called_once()
    outputs[1].assert_called_once()
    publisher.publish_batch.assert_called_once()


@pytest.mark.parametrize("failed", [False, True])
async def test_update_skipped_does_not_prevent_recording_later_results(
    session_factory, sessions, failed
):
    """更新なしとなった結果で処理を止めず、後続の成功結果を記録する。"""
    before = await _seed(session_factory, count=2)
    first, second = before

    async def before_open(number):
        if number == 3:
            async with session_factory() as session:
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.event_id == first)
                    .values(leased_until=PAST)
                )
                await session.commit()

    sessions.before_open = before_open

    def send(envelopes):
        return BatchPublishResult(
            (
                PublishFailed(first, _configuration())
                if failed
                else PublishSucceeded(first),
                PublishSucceeded(second),
            )
        )

    publisher = Mock(publish_batch=Mock(side_effect=send))
    await _relay(sessions, publisher).run_once()
    saved = await _read(session_factory, before)
    assert saved[second]["published_at"] is not None
    assert (3, "rollback") in sessions.trace
    assert (3, "commit") not in sessions.trace


@pytest.mark.parametrize("step", ["stop", "claim", "success", "failure"])
@pytest.mark.parametrize("fault", ["update", "commit", "exit"])
async def test_database_fault_aborts_without_resending_or_undoing_prior_commits(
    session_factory, sessions, outputs, step, fault
):
    """DBエラーで処理を止め、確定済みの結果を残し、その実行内では残りを処理・再送しない。"""
    exhausted = await _seed(session_factory, attempt_count=5)
    before = await _seed(session_factory, count=3)
    sessions.fail_number = {"stop": 1, "claim": 2, "success": 4, "failure": 4}[step]
    sessions.fail_at = fault

    def send(envelopes):
        results = [PublishSucceeded(e.event_id) for e in envelopes]
        if step == "failure":
            results[1] = PublishFailed(envelopes[1].event_id, _configuration())
        return BatchPublishResult(tuple(results))

    publisher = Mock(publish_batch=Mock(side_effect=send))
    with pytest.raises(SQLAlchemyError if fault == "exit" else DatabaseError) as caught:
        await _relay(sessions, publisher).run_once()
    if fault == "exit":
        assert caught.value is sessions.error
    else:
        assert caught.value.__cause__ is sessions.error
    assert not isinstance(caught.value, PublishError)
    assert sessions.count == sessions.fail_number
    saved = list((await _read(session_factory, before)).values())
    if step == "claim":
        stopped = list((await _read(session_factory, exhausted)).values())[0]
        assert stopped["delivery_stopped_at"] is not None
    if step in ("stop", "claim"):
        publisher.publish_batch.assert_not_called()
        assert all(row["published_at"] is None for row in saved)
    else:
        publisher.publish_batch.assert_called_once()
        assert saved[0]["published_at"] is not None
        assert saved[2]["published_at"] is None
        assert saved[2]["delivery_stopped_at"] is None


@pytest.mark.parametrize(
    "case",
    [
        "batch_type",
        "missing",
        "extra",
        "wrong_id",
        "reversed",
        "duplicate",
    ],
)
async def test_invalid_publisher_result_never_partially_updates_events(
    session_factory, sessions, outputs, case
):
    """後半の結果違反もDB更新前に検出し、確保情報だけを残す。"""
    before = await _seed(session_factory, count=2)

    def send(envelopes):
        results = tuple(PublishSucceeded(e.event_id) for e in envelopes)
        options = {
            "batch_type": None,
            "missing": BatchPublishResult(results[:1]),
            "extra": BatchPublishResult(results + (PublishSucceeded(uuid4()),)),
            "wrong_id": BatchPublishResult((results[0], PublishSucceeded(uuid4()))),
            "reversed": BatchPublishResult(results[::-1]),
            "duplicate": BatchPublishResult((results[0], results[0])),
        }
        return options[case]

    publisher = Mock(publish_batch=Mock(side_effect=send))
    with pytest.raises((TypeError, ValueError)) as caught:
        await _relay(sessions, publisher).run_once()
    assert "private body" not in str(caught.value)
    saved = await _read(session_factory, before)
    assert all(
        row["published_at"] is None
        and row["delivery_stopped_at"] is None
        and row["attempt_count"] == 1
        and row["lease_token"]
        for row in saved.values()
    )
    assert sessions.count == 2
    publisher.publish_batch.assert_called_once()
    outputs[0].assert_not_called()


@pytest.mark.parametrize("stage", ["envelope", "publisher"])
@pytest.mark.parametrize(
    "error",
    [ValueError("batch exceeds limit"), RuntimeError("failure"), KeyboardInterrupt()],
)
async def test_call_failure_propagates_without_synthetic_publish_errors(
    session_factory, sessions, outputs, monkeypatch, stage, error
):
    """送信準備や呼び出し自体の失敗を変換せず、確保済みイベントを保持する。"""
    before = await _seed(session_factory)
    publisher = Mock(publish_batch=Mock(side_effect=error))
    if stage == "envelope":
        monkeypatch.setattr(EventEnvelope, "from_claimed", Mock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        await _relay(sessions, publisher).run_once()
    assert caught.value is error
    saved = list((await _read(session_factory, before)).values())[0]
    assert saved["attempt_count"] == 1 and saved["lease_token"]
    assert saved["published_at"] is None and saved["delivery_stopped_at"] is None
    assert publisher.publish_batch.call_count == (1 if stage == "publisher" else 0)
    assert sessions.count == 2
