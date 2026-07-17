"""operator向けpipeline queue status adapterのunit契約。"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts/pipeline_queue_status.py"


def _cli_module() -> ModuleType:
    """未実装scriptをcollection errorではなく契約failureとして報告する。"""
    if not _SCRIPT_PATH.exists():
        pytest.fail("backend/scripts/pipeline_queue_status.py is not implemented")
    module_name = "_test_pipeline_queue_status"
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPT_PATH)
    if spec is None or spec.loader is None:
        pytest.fail("pipeline_queue_status.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == "app.queue.stream_health":
            pytest.fail("app.queue.stream_health is not implemented")
        raise
    return module


def _targets() -> tuple[SimpleNamespace, SimpleNamespace]:
    return (
        SimpleNamespace(stage="curation", stream="pipeline:curation", group="taskiq"),
        SimpleNamespace(
            stage="assessment", stream="pipeline:assessment", group="taskiq"
        ),
    )


def _snapshot(
    stage: str,
    *,
    retained: int = 0,
    lag: int = 0,
    pending: int = 0,
    undelivered_age: float | None = None,
    pending_age: float | None = None,
    outstanding_age: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        stage=stage,
        stream=f"pipeline:{stage}",
        group="taskiq",
        observation_timestamp=1_000.0,
        retained_entries=retained,
        lag=lag,
        pending=pending,
        oldest_undelivered_enqueue_age=undelivered_age,
        oldest_pending_enqueue_age=pending_age,
        oldest_outstanding_enqueue_age=outstanding_age,
    )


class _NoDirectRedisCommands:
    """adapterがhelperを迂回してRedis commandを呼ぶと即失敗する境界。"""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"CLI adapter must not call redis.{name} directly")


def _normalized_header(output: str) -> str:
    first_line = output.splitlines()[0].lower()
    return re.sub(r"[^a-z]+", "_", first_line).strip("_")


@pytest.mark.asyncio
async def test_cli_uses_shared_snapshot_targets_and_empty_age_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _cli_module()
    targets = _targets()
    redis = _NoDirectRedisCommands()
    read_health = AsyncMock(
        side_effect=[_snapshot("curation"), _snapshot("assessment")]
    )
    idle_check = AsyncMock()
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", targets)
    monkeypatch.setattr(module, "read_stream_health", read_health)
    monkeypatch.setattr(module, "has_idle_pending", idle_check)

    output = await module.render_pipeline_queue_status(redis)
    header = _normalized_header(output)
    curation_row = next(
        line for line in output.splitlines() if "pipeline:curation" in line
    )

    assert (
        read_health.await_args_list,
        idle_check.await_count,
        all(
            label in header
            for label in (
                "stream",
                "retained",
                "lag",
                "pending",
                "oldest_undelivered_enqueue_age",
                "oldest_pending_enqueue_age",
                "oldest_outstanding_enqueue_age",
                "status",
            )
        ),
        curation_row.split(),
        "backlog" in output.lower(),
        "queue depth" in output.lower(),
    ) == (
        [call(redis, targets[0]), call(redis, targets[1])],
        0,
        True,
        ["pipeline:curation", "0", "0", "0", "-", "-", "-", "ok"],
        False,
        False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_reason", ["stream_missing", "group_missing"])
async def test_cli_maps_missing_and_unknown_to_nonzero_statuses(
    monkeypatch: pytest.MonkeyPatch,
    missing_reason: str,
) -> None:
    module = _cli_module()
    targets = _targets()
    redis = _NoDirectRedisCommands()
    read_health = AsyncMock(
        side_effect=[
            module.StreamHealthError(stage="curation", reason=missing_reason),
            module.StreamHealthError(stage="assessment", reason="lag_unknown"),
        ]
    )
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", targets)
    monkeypatch.setattr(module, "read_stream_health", read_health)

    output = await module.render_pipeline_queue_status(redis)
    rows = {
        line.split()[0]: line.split()[1:]
        for line in output.splitlines()
        if line.startswith("pipeline:")
    }

    assert rows == {
        "pipeline:curation": ["-", "-", "-", "-", "-", "-", "unavailable"],
        "pipeline:assessment": ["-", "-", "-", "-", "-", "-", "unknown"],
    }


def test_cli_parses_check_idle_as_an_explicit_opt_in_flag() -> None:
    module = _cli_module()

    default_args = module.parse_args([])
    idle_args = module.parse_args(["--check-idle"])

    assert (default_args.check_idle, idle_args.check_idle) == (False, True)


@pytest.mark.asyncio
async def test_cli_maps_redis_and_snapshot_inconsistency_to_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _cli_module()
    targets = _targets()
    redis = _NoDirectRedisCommands()
    read_health = AsyncMock(
        side_effect=[
            module.StreamHealthError(stage="curation", reason="redis_unavailable"),
            module.StreamHealthError(
                stage="assessment", reason="inconsistent_snapshot"
            ),
        ]
    )
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", targets)
    monkeypatch.setattr(module, "read_stream_health", read_health)

    output = await module.render_pipeline_queue_status(redis)
    failure_rows = [
        line.split() for line in output.splitlines() if line.startswith("pipeline:")
    ]

    assert failure_rows == [
        ["pipeline:curation", "-", "-", "-", "-", "-", "-", "failure"],
        ["pipeline:assessment", "-", "-", "-", "-", "-", "-", "failure"],
    ]


@pytest.mark.asyncio
async def test_cli_idle_diagnostic_is_opt_in_existence_not_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _cli_module()
    targets = _targets()
    redis = _NoDirectRedisCommands()
    read_health = AsyncMock(
        side_effect=[_snapshot("curation"), _snapshot("assessment")]
    )
    idle_check = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", targets)
    monkeypatch.setattr(module, "read_stream_health", read_health)
    monkeypatch.setattr(module, "has_idle_pending", idle_check)

    output = await module.render_pipeline_queue_status(redis, check_idle=True)

    assert (
        idle_check.await_args_list,
        "idle>=600s entry exists" in output.lower(),
        "maximum idle" in output.lower(),
        "max idle" in output.lower(),
    ) == (
        [
            call(redis, targets[0], idle_ms=600_000),
            call(redis, targets[1], idle_ms=600_000),
        ],
        True,
        False,
        False,
    )
