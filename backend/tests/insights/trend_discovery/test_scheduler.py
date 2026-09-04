"""Trend Discovery scheduler producerの所有権テスト。"""

from __future__ import annotations

import subprocess
import sys

from taskiq import TaskiqEvents

from app.insights.trend_discovery.scheduler import create_scheduler


def test_create_scheduler_builds_distinct_producer_brokers() -> None:
    first = create_scheduler()
    second = create_scheduler()

    assert first is not second
    assert first.broker is not second.broker
    assert first.broker.connection_pool is not second.broker.connection_pool
    assert first.broker.queue_name == "trend_discovery"
    assert set(first.broker.get_all_tasks()) == {"run_trend_discovery"}
    assert len(first.broker.event_handlers[TaskiqEvents.CLIENT_STARTUP]) == 1
    assert len(first.broker.event_handlers[TaskiqEvents.CLIENT_SHUTDOWN]) == 1
    assert first.broker.event_handlers[TaskiqEvents.WORKER_STARTUP] == []


def test_scheduler_import_does_not_load_global_broker_catalog() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from app.insights.trend_discovery.scheduler import create_scheduler; "
                "create_scheduler(); "
                "print('app.queue.brokers' in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
