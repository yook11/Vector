"""Trend Discovery worker applicationの所有権テスト。"""

from __future__ import annotations

import configparser
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskiq import TaskiqEvents, TaskiqState

from app.config import settings
from app.insights.trend_discovery.worker import create_broker

_SUPERVISORD_CONFIG = (
    Path(__file__).resolve().parents[3] / "supervisord" / "insights.conf"
)


def test_create_broker_builds_only_trend_discovery_runtime() -> None:
    first = create_broker()
    second = create_broker()

    assert first is not second
    assert first.connection_pool is not second.connection_pool
    assert first.queue_name == "trend_discovery"
    assert set(first.get_all_tasks()) == {"run_trend_discovery"}
    assert len(first.event_handlers[TaskiqEvents.WORKER_STARTUP]) == 1
    assert len(first.event_handlers[TaskiqEvents.WORKER_SHUTDOWN]) == 1


def test_worker_import_does_not_load_global_broker_catalog() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from app.insights.trend_discovery.worker import create_broker; "
                "create_broker(); "
                "print('app.queue.brokers' in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


@pytest.mark.asyncio
async def test_worker_lifecycle_owns_engine_and_session_factory() -> None:
    broker = create_broker()
    state = TaskiqState()
    engine = MagicMock()
    engine.dispose = AsyncMock()
    session_factory = MagicMock()

    with (
        patch("app.insights.trend_discovery.worker.setup_logfire") as setup_logfire,
        patch(
            "app.insights.trend_discovery.worker.create_worker_engine",
            return_value=engine,
        ) as build_engine,
        patch(
            "app.insights.trend_discovery.worker.caller_managed_session_factory",
            return_value=session_factory,
        ),
        patch("app.insights.trend_discovery.worker.logfire.instrument_sqlalchemy"),
        patch(
            "app.insights.trend_discovery.worker.log_pool_initialized"
        ) as log_pool_initialized,
        patch("app.insights.trend_discovery.worker.register_pool_metrics"),
    ):
        await broker.event_handlers[TaskiqEvents.WORKER_STARTUP][0](state)
        await broker.event_handlers[TaskiqEvents.WORKER_SHUTDOWN][0](state)

    setup_logfire.assert_called_once_with("vector-worker-trend_discovery")
    build_engine.assert_called_once_with(settings, "trend_discovery")
    log_pool_initialized.assert_called_once_with(
        service_name="vector-worker-trend_discovery",
        pool_size=2,
        max_overflow=2,
        pool_recycle=240,
        pool_timeout=5,
    )
    assert state.engine is engine
    assert state.session_factory is session_factory
    engine.dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_startup_failure_closes_created_engine() -> None:
    broker = create_broker()
    state = TaskiqState()
    engine = MagicMock()
    engine.dispose = AsyncMock()

    with (
        patch("app.insights.trend_discovery.worker.setup_logfire"),
        patch(
            "app.insights.trend_discovery.worker.create_worker_engine",
            return_value=engine,
        ),
        patch("app.insights.trend_discovery.worker.caller_managed_session_factory"),
        patch(
            "app.insights.trend_discovery.worker.logfire.instrument_sqlalchemy",
            side_effect=RuntimeError("instrumentation failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="instrumentation failed"):
            await broker.event_handlers[TaskiqEvents.WORKER_STARTUP][0](state)

    engine.dispose.assert_awaited_once_with()
    assert not hasattr(state, "engine")


def test_supervisor_uses_context_owned_broker_factory() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_CONFIG)
    command = shlex.split(parser["program:trend-discovery"]["command"])

    assert command == [
        "taskiq",
        "worker",
        "--workers",
        "1",
        "--max-async-tasks",
        "2",
        "app.insights.trend_discovery.worker:create_broker",
        "--ack-type",
        "when_executed",
    ]
