"""ローカルの配送シナリオとは重ならない例外・資源の境界を検証する。"""

from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError

from app.collection.article_acquisition.acquisition_dispatch import (
    SourceAcquisitionDispatcher,
)
from app.collection.sources.acquisition_request import SourceAcquisitionSchedule


@pytest.mark.asyncio
async def test_database_failure_propagates_before_opening_sender():
    """対象選定のDB障害は元の例外のまま伝播し、送信を開始しない。"""
    error = OperationalError(None, None, RuntimeError("database unavailable"))
    selection = Mock(select=AsyncMock(side_effect=error))
    sender_factory = Mock()
    dispatcher = SourceAcquisitionDispatcher(
        dispatch_service=selection, sender_factory=sender_factory
    )
    schedule = SourceAcquisitionSchedule.model_validate(
        {"cadence": "high", "scheduled_at": "2026-09-13T01:00:00Z"}
    )
    with pytest.raises(OperationalError) as caught:
        await dispatcher.dispatch(schedule)
    assert caught.value is error
    sender_factory.assert_not_called()
