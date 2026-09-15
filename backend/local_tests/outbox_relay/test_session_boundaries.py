"""Relayが準備用sessionを閉じてから送信することを確認する。"""

from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest

from app.outbox.publishing.publisher import BatchPublishResult, PublishSucceeded
from local_tests.outbox_relay.support import (
    make_relay,
    read_saved,
    seed_event,
)

pytestmark = pytest.mark.asyncio


async def test_stop_is_visible_before_claim_opens(owner_sessions, relay_sessions):
    """停止のcommitは、確保用sessionを開く前に別接続から見える。"""
    exhausted = await seed_event(owner_sessions, attempt_count=5)
    finished_stop = False

    @asynccontextmanager
    async def sessions():
        nonlocal finished_stop
        if finished_stop:
            saved = await read_saved(owner_sessions, exhausted)
            assert saved["delivery_stopped_at"] is not None
        async with relay_sessions() as session:
            yield session
        finished_stop = True

    await make_relay(sessions, Mock()).run_once()


async def test_preparation_sessions_are_closed_before_send(
    owner_sessions, relay_sessions, relay_engine
):
    """停止・確保の接続は、送信時点では貸し出されていない。"""
    ready = await seed_event(owner_sessions)

    def send(envelopes):
        assert relay_engine.pool.checkedout() == 0
        assert [envelope.event_id for envelope in envelopes] == [ready.event_id]
        return BatchPublishResult((PublishSucceeded(ready.event_id),))

    publisher = Mock(publish_batch=Mock(side_effect=send))
    await make_relay(relay_sessions, publisher).run_once()
    publisher.publish_batch.assert_called_once()
