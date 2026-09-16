"""1回の実行がrepositoryへ渡す対象と件数を検証する。"""

from datetime import timedelta
from unittest.mock import Mock, call

import pytest

from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.relay import OutboxRelay
from app.outbox.delivery.repository import OutboxDeliveryRepository
from app.outbox.delivery.values import DeliveryBatchSelection, LeaseDuration

pytestmark = pytest.mark.asyncio


async def test_relay_passes_event_type_and_processing_limits(
    session_factory, monkeypatch
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
    await OutboxRelay(
        session_factory,
        Mock(),
        OutboxDeliveryFailureHandler(session_factory),
        event_type="test.delivery",
    ).run_once()
    assert calls.mock_calls == [
        call.stop(
            selection=DeliveryBatchSelection(event_type="test.delivery", limit=100)
        ),
        call.claim(
            selection=DeliveryBatchSelection(event_type="test.delivery", limit=10),
            lease_duration=LeaseDuration(timedelta(seconds=150)),
        ),
    ]
