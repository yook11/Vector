"""agentだけの予約更新間隔と終了時の接続解放。"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.queue import scheduler_entrypoint as entrypoint


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [False, True])
async def test_scheduler_interval_and_cleanup_despite_broker_shutdown_failure(
    monkeypatch, agent
):
    # 予約sourceはbroker終了エラーでも閉じ、他schedulerの間隔を保つ。
    source = MagicMock(startup=AsyncMock(), shutdown=AsyncMock())
    scheduler = MagicMock(
        sources=[source],
        startup=AsyncMock(),
        shutdown=AsyncMock(side_effect=RuntimeError("shutdown")),
    )
    loop = MagicMock(run=AsyncMock())
    monkeypatch.setattr(entrypoint, "SchedulerLoop", lambda _: loop)
    if agent:
        monkeypatch.setattr(entrypoint, "scheduler_agent", scheduler)
    with pytest.raises(RuntimeError, match="shutdown"):
        await entrypoint._run_one(scheduler)
    assert loop.run.await_args.kwargs["update_interval"] == timedelta(
        seconds=1 if agent else 60
    )
    source.startup.assert_awaited_once()
    source.shutdown.assert_awaited_once()
