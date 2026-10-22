"""1回の実行がrepositoryへ渡す対象と件数を検証する。"""

from datetime import timedelta
from unittest.mock import Mock, call

import pytest

from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.relay import OutboxRelay
from app.outbox.delivery.repository import OutboxDeliveryRepository
from app.outbox.delivery.values import LeaseDuration
from app.outbox.publishing.route import EventDeliveryRoute

pytestmark = pytest.mark.asyncio


async def test_relay_passes_event_type_and_processing_limits(
    session_factory, monkeypatch
):
    """対象タイプ・停止100件・確保10件・lease150秒をrepositoryへ渡す。"""
    calls = Mock()
    stop = OutboxDeliveryRepository.stop_deliveries_at_attempt_limit
    lease = OutboxDeliveryRepository.lease_events_to_publish

    async def observe_stop(repository, **kwargs):
        calls.stop(**kwargs)
        return await stop(repository, **kwargs)

    async def observe_lease(repository, **kwargs):
        calls.lease(**kwargs)
        return await lease(repository, **kwargs)

    monkeypatch.setattr(
        OutboxDeliveryRepository, "stop_deliveries_at_attempt_limit", observe_stop
    )
    monkeypatch.setattr(
        OutboxDeliveryRepository, "lease_events_to_publish", observe_lease
    )
    await OutboxRelay(
        session_factory,
        Mock(),
        OutboxDeliveryFailureHandler(session_factory),
        route=EventDeliveryRoute("test.delivery", "https://sqs.invalid/test"),
        build_message=Mock(),
    ).run_once()
    assert calls.mock_calls == [
        call.stop(event_type="test.delivery", limit=100),
        call.lease(
            event_type="test.delivery",
            limit=10,
            lease_duration=LeaseDuration(timedelta(seconds=150)),
        ),
    ]
