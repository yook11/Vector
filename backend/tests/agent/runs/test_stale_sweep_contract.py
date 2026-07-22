"""stale sweep の時間・結果・observability 契約。"""

from __future__ import annotations

import ast
import inspect
import textwrap
from uuid import UUID

import pytest

import app.agent.runs.contracts as run_contracts
import app.agent.runs.daily_quota.observability as quota_observability
import app.agent.runs.repository as run_repository
import app.queue.schedule as schedule
import app.queue.tasks.agent_run as agent_run_tasks


def test_stale_sweep_uses_fixed_named_time_policy_and_minute_schedule() -> None:
    assert getattr(run_repository, "RESEARCH_RUNNING_STALE_AFTER_SECONDS", None) == 180
    assert getattr(run_repository, "RESEARCH_QUEUED_STALE_AFTER_SECONDS", None) == 300
    assert getattr(schedule, "RESEARCH_STALE_SWEEP_CRON", None) == "* * * * *"
    assert schedule.CRON_AGENT_RUN_SWEEP == schedule.RESEARCH_STALE_SWEEP_CRON
    assert agent_run_tasks.CRON_AGENT_RUN_SWEEP == schedule.RESEARCH_STALE_SWEEP_CRON
    assert (
        "* * * * *          | 毎分         | 毎分         | sweep_stale_agent_runs"
        in (inspect.getdoc(schedule) or "")
    )


def test_stale_sweep_task_decorator_reuses_schedule_source_of_truth() -> None:
    tree = ast.parse(inspect.getsource(agent_run_tasks))
    task = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "sweep_stale_agent_runs"
    )
    decorator = next(
        node
        for node in task.decorator_list
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "task"
    )
    schedule_value = next(
        keyword.value for keyword in decorator.keywords if keyword.arg == "schedule"
    )

    assert isinstance(schedule_value, ast.List)
    assert isinstance(schedule_value.elts[0], ast.Dict)
    cron_value = schedule_value.elts[0].values[0]
    assert isinstance(cron_value, ast.Name)
    assert cron_value.id == "CRON_AGENT_RUN_SWEEP"


def test_stale_sweep_derives_its_default_cutoff_from_database_time() -> None:
    tree = ast.parse(
        textwrap.dedent(
            inspect.getsource(run_repository.AgentRunRepository.sweep_stale_runs)
        )
    )
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    loaded_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }

    assert {
        "RESEARCH_QUEUED_STALE_AFTER_SECONDS",
        "RESEARCH_RUNNING_STALE_AFTER_SECONDS",
    } <= loaded_names
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "datetime"
        and node.attr == "now"
    ]
    assert any(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "func"
        and call.func.attr in {"statement_timestamp", "transaction_timestamp", "now"}
        for call in calls
    )


def test_stale_sweep_result_keeps_queued_and_running_outcomes_distinct() -> None:
    running_run_type = getattr(run_contracts, "StaleRunningRun", None)
    assert running_run_type is not None
    running_run = running_run_type(
        run_id=UUID("00000000-0000-4000-a000-000000000801"),
        attempt_epoch=3,
    )
    result = run_contracts.StaleRunSweepResult(
        queued_terminal_count=4,
        queued_quota_released_count=2,
        queued_quota_not_eligible_count=1,
        queued_quota_inconsistent_count=1,
        running_terminal_runs=(running_run,),
        running_quota_reservation_count=1,
        running_without_started_at_count=1,
    )

    assert result.queued_terminal_count == 4
    assert result.queued_quota_released_count == 2
    assert result.queued_quota_not_eligible_count == 1
    assert result.queued_quota_inconsistent_count == 1
    assert result.running_terminal_runs == (running_run,)
    assert result.running_quota_reservation_count == 1
    assert result.running_without_started_at_count == 1


def test_stale_sweep_result_rejects_nonpositive_running_attempt_epoch() -> None:
    running_run_type = getattr(run_contracts, "StaleRunningRun", None)
    assert running_run_type is not None

    with pytest.raises(ValueError, match="positive attempt epoch"):
        running_run_type(
            run_id=UUID("00000000-0000-4000-a000-000000000802"),
            attempt_epoch=0,
        )


def test_daily_quota_release_metric_accepts_aggregated_positive_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, dict[str, str]]] = []

    class Counter:
        def add(self, count: int, *, attributes: dict[str, str]) -> None:
            calls.append((count, attributes))

    monkeypatch.setattr(
        quota_observability,
        "_daily_quota_releases_counter",
        Counter(),
    )

    quota_observability.record_daily_quota_release(result="released", count=3)
    quota_observability.record_daily_quota_release(result="not_eligible")

    assert calls == [
        (3, {"result": "released"}),
        (1, {"result": "not_eligible"}),
    ]
