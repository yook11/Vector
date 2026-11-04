"""agent の queue タスクが、失敗を taskiq へ渡さずに終えることを確かめる。

DBは使わず、本物の OpenTelemetryMiddleware を付けた InMemoryBroker に
宣言どおりのタスクを登録し、ドメインの入口を差し替えて実行する。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode
from taskiq import AsyncTaskiqDecoratedTask, InMemoryBroker
from taskiq.middlewares.opentelemetry_middleware import OpenTelemetryMiddleware

import app.queue.tasks.agent_run as agent_run_tasks
from app.queue.messages.agent_run import AgentRunTrigger
from tests.agent._sensitive_db_failure import (
    SensitivePersistenceFailure,
    assert_no_sensitive_marker,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000010")


async def _execute_declared_task(
    task: AsyncTaskiqDecoratedTask[Any, Any],
    capfire: CaptureLogfire,
    caplog: pytest.LogCaptureFixture,
    **kwargs: object,
) -> tuple[bool, list[str], StatusCode]:
    broker = InMemoryBroker(await_inplace=True).with_middlewares(
        OpenTelemetryMiddleware()
    )
    broker.state.session_factory = object()
    broker.state.agent_live_redis = object()
    broker.state.agent_deadline_scheduler = SimpleNamespace(
        reserve_in_background=lambda *_: None
    )
    registered = broker.register_task(
        task.original_func, task_name=task.task_name, **task.labels
    )
    await broker.startup()
    try:
        with caplog.at_level(logging.ERROR, logger="taskiq.receiver.receiver"):
            sent = await registered.kiq(**kwargs)
            result = await sent.wait_result()
    finally:
        await broker.shutdown()
    [span] = [
        span
        for span in capfire.exporter.exported_spans
        if span.name == f"execute/{task.task_name}"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    receiver_errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == "taskiq.receiver.receiver"
    ]
    return result.is_err, receiver_errors, span.status.status_code


@pytest.mark.parametrize(
    ("task", "domain_entry", "kwargs"),
    [
        pytest.param(
            agent_run_tasks.run_agent_answer,
            "execute_run_attempt",
            {"trigger": AgentRunTrigger(run_id=_RUN_ID)},
            id="run",
        ),
        pytest.param(
            agent_run_tasks.sweep_deadline_exceeded_agent_runs,
            "sweep_and_report_deadline_exceeded_runs",
            {},
            id="sweep",
        ),
        pytest.param(
            agent_run_tasks.check_agent_run_deadline,
            "check_and_report_run_deadline",
            {"trigger": AgentRunTrigger(run_id=_RUN_ID)},
            id="check",
        ),
    ],
)
@pytest.mark.asyncio
async def test_domain_failure_closes_execute_span_as_error_without_reaching_taskiq(
    task: AsyncTaskiqDecoratedTask[Any, Any],
    domain_entry: str,
    kwargs: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
    caplog: pytest.LogCaptureFixture,
    read_logs,
) -> None:
    """ドメインから秘匿値つきのDB例外が出ても、taskiqには成功として返り、実行のspanは失敗で閉じ、ログに秘匿値が出ない。"""

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise SensitivePersistenceFailure

    monkeypatch.setattr(agent_run_tasks, domain_entry, fail)

    outcome = await _execute_declared_task(task, capfire, caplog, **kwargs)

    assert outcome == (False, [], StatusCode.ERROR)
    assert_no_sensitive_marker(read_logs())


@pytest.mark.parametrize(
    ("task", "domain_entry"),
    [
        pytest.param(agent_run_tasks.run_agent_answer, "execute_run_attempt", id="run"),
        pytest.param(
            agent_run_tasks.check_agent_run_deadline,
            "check_and_report_run_deadline",
            id="check",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_trigger_ends_without_domain_call_or_reaching_taskiq(
    task: AsyncTaskiqDecoratedTask[Any, Any],
    domain_entry: str,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """taskiqが解釈できずにそのまま渡す引数(run_idがUUIDでない)では、ドメインを呼ばず、taskiqには成功として返り、実行のspanは失敗で閉じる。"""
    calls: list[object] = []

    async def record(*args: object, **_kwargs: object) -> None:
        calls.append(args)

    monkeypatch.setattr(agent_run_tasks, domain_entry, record)

    outcome = await _execute_declared_task(
        task, capfire, caplog, trigger={"run_id": "not-a-uuid"}
    )

    assert (outcome, calls) == ((False, [], StatusCode.ERROR), [])
