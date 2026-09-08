"""配信判断とDB確定の境界を実DBで検証する。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.errors import DatabaseError
from app.db.session import caller_managed_session_factory
from app.http.failure import HttpTransportFailure, HttpTransportFailureKind
from app.outbox import publish_failure_handler as handler_module
from app.outbox.publish_errors import (
    PublishCleanupError,
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishTransportError,
)
from app.outbox.publish_failure_handler import (
    DeliveryStopped,
    DeliveryUpdateSkipped,
    PublishFailureHandler,
    RetryScheduled,
)
from app.outbox.publish_retry_policy import NonRetryableReason
from app.outbox.publisher import PublishFailed, PublishSucceeded
from app.outbox.repository import ClaimedOutboxEvent
from tests.outbox.helpers import FUTURE, PAST, insert_event, read_event

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def record_stop(monkeypatch):
    record = Mock()
    monkeypatch.setattr(handler_module, "record_publish_failure", record)
    return record


def _transport():
    return PublishTransportError(
        failure=HttpTransportFailure(HttpTransportFailureKind.READ_TIMEOUT, True)
    )


def _configuration():
    return PublishConfigurationError(
        reason=PublishConfigurationReason.MISSING_CREDENTIALS
    )


def _claimed(event_id=None, **changes):
    values = dict(
        event_id=event_id or uuid4(),
        event_type="test.outbox",
        schema_version=1,
        payload={"article_id": 123},
        occurred_at=PAST,
        attempt_count=1,
        lease_token=uuid4(),
        leased_until=FUTURE,
    )
    values.update(changes)
    return ClaimedOutboxEvent(**values)


async def _seed(session_factory, **changes):
    token = uuid4()
    values = dict(
        next_attempt_at=PAST,
        lease_token=token,
        leased_until=FUTURE,
        attempt_count=1,
        payload={"article_id": 123},
    )
    values.update(changes)
    async with session_factory() as session:
        event_id = await insert_event(session, **values)
        before = await read_event(session, event_id)
        await session.commit()
    claimed = _claimed(
        event_id, lease_token=token, attempt_count=values["attempt_count"]
    )
    return claimed, before


@pytest.mark.parametrize(
    ("error", "attempt", "expected"),
    [
        (_transport(), 1, RetryScheduled(timedelta(seconds=30))),
        (_transport(), 4, RetryScheduled(timedelta(minutes=30))),
        (
            _configuration(),
            1,
            DeliveryStopped(NonRetryableReason.NON_RETRYABLE_FAILURE),
        ),
        (_transport(), 5, DeliveryStopped(NonRetryableReason.RETRY_EXHAUSTED)),
    ],
)
async def test_result_means_committed_update_visible_from_another_session(
    session_factory,
    error,
    attempt,
    expected,
    record_stop,
):
    """確定結果とlease解除を別sessionから読み、無関係な列の維持も確認する。"""
    claimed, before = await _seed(session_factory, attempt_count=attempt)
    jitter = Mock(return_value=0.5)
    async with session_factory() as reader:
        start = await reader.scalar(select(func.statement_timestamp()))
        engine = reader.bind
    handler = PublishFailureHandler(
        caller_managed_session_factory(engine), jitter=jitter
    )
    assert (
        await handler.handle(
            event=claimed, failure=PublishFailed(claimed.event_id, error)
        )
        == expected
    )
    jitter.assert_called_once_with()
    if isinstance(expected, DeliveryStopped):
        record_stop.assert_called_once_with(
            event=claimed, error=error, stop_reason=expected.reason
        )
    else:
        record_stop.assert_not_called()
    record_stop.reset_mock()
    async with session_factory() as reader:
        saved = await read_event(reader, claimed.event_id)
        end = await reader.scalar(select(func.statement_timestamp()))
    changes = {"lease_token": None, "leased_until": None}
    if isinstance(expected, RetryScheduled):
        assert (
            start + expected.delay <= saved["next_attempt_at"] <= end + expected.delay
        )
        changes["next_attempt_at"] = saved["next_attempt_at"]
    else:
        assert start <= saved["delivery_stopped_at"] <= end
        changes.update(
            delivery_stopped_at=saved["delivery_stopped_at"],
            delivery_stop_reason=expected.reason.value,
        )
    assert saved == {**before, **changes}
    assert (
        await handler.handle(
            event=claimed, failure=PublishFailed(claimed.event_id, error)
        )
        == DeliveryUpdateSkipped()
    )
    record_stop.assert_not_called()
    async with session_factory() as reader:
        assert await read_event(reader, claimed.event_id) == saved


@pytest.mark.parametrize("error", [_transport(), _configuration()])
@pytest.mark.parametrize(
    "case",
    [
        "expired",
        "wrong_token",
        "unclaimed",
        "published",
        "stopped",
        "missing",
    ],
)
async def test_invalid_update_conditions_skip_without_modifying_rows(
    session_factory,
    error,
    case,
    record_stop,
):
    """更新不能の原因を推測せず、他の行や既存の配信結果を維持する。"""
    changes = {
        "expired": {"leased_until": PAST},
        "unclaimed": {"lease_token": None, "leased_until": None},
        "published": {"published_at": PAST},
        "stopped": {"delivery_stopped_at": PAST, "delivery_stop_reason": "existing"},
    }.get(case, {})
    claimed, before = await _seed(session_factory, **changes)
    actual_id = claimed.event_id
    if case == "wrong_token":
        claimed = replace(claimed, lease_token=uuid4())
    if case == "missing":
        claimed = replace(claimed, event_id=uuid4())
    rollbacks = []
    commits = []

    @asynccontextmanager
    async def factory():
        async with session_factory() as session:
            sqlalchemy_event.listen(
                session.sync_session, "after_rollback", lambda _: rollbacks.append(True)
            )
            sqlalchemy_event.listen(
                session.sync_session, "after_commit", lambda _: commits.append(True)
            )
            yield session

    result = await PublishFailureHandler(factory, jitter=lambda: 0.5).handle(
        event=claimed, failure=PublishFailed(claimed.event_id, error)
    )
    assert result == DeliveryUpdateSkipped()
    record_stop.assert_not_called()
    assert rollbacks == [True]
    assert commits == []
    async with session_factory() as reader:
        assert await read_event(reader, actual_id) == before


@pytest.mark.parametrize("error", [_transport(), _configuration()])
@pytest.mark.parametrize("stage", ["update", "commit"])
async def test_database_failure_propagates_and_uncommitted_update_is_rolled_back(
    session_factory,
    error,
    stage,
    record_stop,
):
    """実UPDATE後またはcommit直前の障害を注入し、変更の取り消しを確認する。"""
    claimed, before = await _seed(session_factory)
    async with session_factory() as session:
        engine = session.bind
    original = SQLAlchemyError("injected database failure")

    def fail(*args, **kwargs):
        raise original

    @asynccontextmanager
    async def factory():
        async with caller_managed_session_factory(engine)() as session:
            if stage == "commit":
                sqlalchemy_event.listen(session.sync_session, "before_commit", fail)
            else:
                connection = await session.connection()
                sqlalchemy_event.listen(
                    connection.sync_connection, "after_cursor_execute", fail, once=True
                )
            yield session

    with pytest.raises(DatabaseError) as caught:
        await PublishFailureHandler(factory, jitter=lambda: 0.5).handle(
            event=claimed, failure=PublishFailed(claimed.event_id, error)
        )
    record_stop.assert_not_called()
    assert not isinstance(caught.value, PublishError)
    assert caught.value.__cause__ is original
    async with session_factory() as reader:
        assert await read_event(reader, claimed.event_id) == before


async def test_session_exit_failure_does_not_return_success_or_undo_commit(
    session_factory,
    record_stop,
):
    """commit後の終了失敗では成功結果を返さず、保存済みの停止は維持する。"""
    claimed, _ = await _seed(session_factory)
    failure = RuntimeError("session cleanup")

    @asynccontextmanager
    async def factory():
        async with session_factory() as session:
            yield session
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await PublishFailureHandler(factory, jitter=lambda: 0.5).handle(
            event=claimed, failure=PublishFailed(claimed.event_id, _configuration())
        )
    record_stop.assert_not_called()
    assert caught.value is failure
    async with session_factory() as reader:
        saved = await read_event(reader, claimed.event_id)
    assert saved["delivery_stopped_at"] is not None
    assert saved["lease_token"] is None


@pytest.mark.parametrize(
    ("error", "attempt", "jitter", "exception_type"),
    [
        (
            PublishCleanupError(original_exception=RuntimeError()),
            1,
            0.5,
            TypeError,
        ),
        (RuntimeError("db failure"), 1, 0.5, TypeError),
        (_transport(), 0, 0.5, ValueError),
        (_transport(), True, 0.5, TypeError),
        (_transport(), 1, float("nan"), ValueError),
        (_transport(), 1, -0.1, ValueError),
        (_transport(), 1, 1.1, ValueError),
        (_transport(), 1, "bad", TypeError),
    ],
)
async def test_invalid_policy_input_never_opens_session(
    error,
    attempt,
    jitter,
    exception_type,
    record_stop,
):
    """判断対象外の入力はDBに触れる前に拒否する。"""
    factory = Mock()
    generate = Mock(return_value=jitter)
    claimed = _claimed(attempt_count=attempt)
    with pytest.raises(exception_type):
        await PublishFailureHandler(factory, jitter=generate).handle(
            event=claimed, failure=PublishFailed(claimed.event_id, error)
        )
    record_stop.assert_not_called()
    factory.assert_not_called()
    generate.assert_called_once_with()


@pytest.mark.parametrize(
    "failure", [RuntimeError("random failed"), KeyboardInterrupt()]
)
async def test_jitter_generation_failure_is_not_reclassified(failure, record_stop):
    """乱数生成の失敗やプロセス終了を配信失敗へ変換しない。"""
    factory = Mock()
    claimed = _claimed()
    with pytest.raises(type(failure)) as caught:
        await PublishFailureHandler(factory, jitter=Mock(side_effect=failure)).handle(
            event=claimed, failure=PublishFailed(claimed.event_id, _transport())
        )
    record_stop.assert_not_called()
    assert caught.value is failure
    factory.assert_not_called()


@pytest.mark.parametrize("case", ["success", "raw_error", "none", "wrong_id"])
async def test_invalid_failure_is_rejected_before_jitter_session_or_record(
    case, record_stop
):
    """別イベントの失敗や失敗結果以外の入力で副作用を起こさない。"""
    claimed = _claimed()
    failure = {
        "success": PublishSucceeded(claimed.event_id),
        "raw_error": _configuration(),
        "none": None,
        "wrong_id": PublishFailed(uuid4(), _configuration()),
    }[case]
    factory = Mock()
    jitter = Mock(return_value=0.5)
    with pytest.raises(ValueError if case == "wrong_id" else TypeError):
        await PublishFailureHandler(factory, jitter=jitter).handle(
            event=claimed, failure=failure
        )
    jitter.assert_not_called()
    factory.assert_not_called()
    record_stop.assert_not_called()
