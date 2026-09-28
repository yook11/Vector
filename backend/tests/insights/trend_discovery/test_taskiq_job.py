"""Taskiq入口の資源引き渡し・登録情報・spanを確認する。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from taskiq import InMemoryBroker

from app.audit.domain.event import Stage
from tests.logfire._span_helpers import pipeline_stage_attrs


def test_registers_daily_task_with_existing_retry_and_timeout():
    """指定brokerに既存のcron・timeout・retry条件で登録する。"""
    from app.insights.trend_discovery import taskiq_job

    broker = InMemoryBroker()
    task = taskiq_job.register_trend_discovery_task(broker)
    assert task.task_name == "run_trend_discovery"
    assert task.labels["timeout"] == 600
    assert task.labels["max_retries"] == 0
    assert task.labels["retry_on_error"] is False
    assert task.labels["schedule"] == [{"cron": "5 15 * * *"}]
    assert task.broker is broker


@pytest.mark.asyncio
async def test_passes_worker_session_factory_and_notifier_to_service():
    """workerのsession factoryと通知先をServiceへ渡す。"""
    from app.insights.trend_discovery import taskiq_job

    ctx = MagicMock()
    with (
        patch.object(taskiq_job, "TrendDiscoveryService") as service_type,
        patch.object(
            taskiq_job.FrontendRevalidateNotifier, "from_settings"
        ) as notifier,
    ):
        service_type.return_value.create = AsyncMock()
        await taskiq_job.run_trend_discovery(ctx=ctx)
    service_type.assert_called_once_with(ctx.state.session_factory)
    service_type.return_value.create.assert_awaited_once_with(notifier.return_value)


@pytest.mark.asyncio
async def test_propagates_service_failure():
    """Serviceの失敗をTaskiqへ伝播する。"""
    from app.insights.trend_discovery import taskiq_job

    with (
        patch.object(
            taskiq_job.TrendDiscoveryService,
            "create",
            new=AsyncMock(side_effect=RuntimeError("failed")),
        ),
        pytest.raises(RuntimeError, match="failed"),
    ):
        await taskiq_job.run_trend_discovery(ctx=MagicMock())


@pytest.mark.asyncio
async def test_stage_span_identifies_trend_discovery(capfire: CaptureLogfire):
    """タスク実行をtrend_discoveryのspanへ記録する。"""
    from app.insights.trend_discovery import taskiq_job

    with patch.object(taskiq_job.TrendDiscoveryService, "create", new=AsyncMock()):
        await taskiq_job.run_trend_discovery(ctx=MagicMock())
    attrs = pipeline_stage_attrs(capfire)
    assert attrs["stage"] == Stage.TREND_DISCOVERY.value
    assert attrs["op"] == "run_trend_discovery"
