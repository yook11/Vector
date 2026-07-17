import datetime as dt
from collections.abc import Mapping
from itertools import pairwise
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from scripts import seed_e2e_research as seed_script

FIXTURE_THREADS = seed_script.FIXTURE_THREADS
guard_production = seed_script.guard_production

_CONTINUITY_IDS = {
    "closed": {
        "thread_id": UUID("00000000-0000-4000-a000-00000000e2d4"),
        "completed_user_message_id": UUID("00000000-0000-4000-a000-00000000d401"),
        "assistant_message_id": UUID("00000000-0000-4000-a000-00000000d4a1"),
        "completed_run_id": UUID("00000000-0000-4000-a000-00000000d4f1"),
        "active_user_message_id": UUID("00000000-0000-4000-a000-00000000d402"),
        "active_run_id": UUID("00000000-0000-4000-a000-00000000d4f2"),
    },
    "open": {
        "thread_id": UUID("00000000-0000-4000-a000-00000000e2e5"),
        "completed_user_message_id": UUID("00000000-0000-4000-a000-00000000e501"),
        "assistant_message_id": UUID("00000000-0000-4000-a000-00000000e5a1"),
        "completed_run_id": UUID("00000000-0000-4000-a000-00000000e5f1"),
        "active_user_message_id": UUID("00000000-0000-4000-a000-00000000e502"),
        "active_run_id": UUID("00000000-0000-4000-a000-00000000e5f2"),
    },
}


def _fixture_value(fixture: Any, field: str) -> Any:
    if isinstance(fixture, Mapping):
        return fixture[field]
    return getattr(fixture, field)


def _continuity_fixtures() -> dict[str, Any]:
    fixtures = seed_script.CONTINUITY_FIXTURES
    if isinstance(fixtures, Mapping):
        normalized = dict(fixtures)
    else:
        normalized = {
            _fixture_value(fixture, "variant"): fixture for fixture in fixtures
        }
    assert set(normalized) == {"closed", "open"}
    assert len(normalized) == 2
    return normalized


def _batched_rows(execute: AsyncMock) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for await_call in execute.await_args_list:
        if len(await_call.args) < 2:
            continue
        parameters = await_call.args[1]
        if isinstance(parameters, Mapping):
            rows.append(dict(parameters))
        elif isinstance(parameters, (list, tuple)):
            rows.extend(parameters)
    return rows


def test_fixture_has_core_and_history_threads_in_deterministic_order() -> None:
    assert len(FIXTURE_THREADS) == 20
    assert [(thread.label, thread.thread_id) for thread in FIXTURE_THREADS[:3]] == [
        ("A", UUID("00000000-0000-4000-a000-00000000e2a1")),
        ("B", UUID("00000000-0000-4000-a000-00000000e2b2")),
        ("C", UUID("00000000-0000-4000-a000-00000000e2c3")),
    ]
    assert [thread.label for thread in FIXTURE_THREADS[3:]] == [
        f"HISTORY_{index:02d}" for index in range(1, 18)
    ]
    assert all(
        newer.updated_at > older.updated_at
        for newer, older in pairwise(FIXTURE_THREADS)
    )
    all_ids = [
        getattr(thread, field)
        for thread in FIXTURE_THREADS
        for field in (
            "thread_id",
            "user_message_id",
            "assistant_message_id",
            "run_id",
        )
    ]
    assert len(all_ids) == 80 == len(set(all_ids))


def test_continuity_fixtures_have_fixed_disjoint_ids_and_order() -> None:
    fixtures = _continuity_fixtures()

    for variant, expected_ids in _CONTINUITY_IDS.items():
        fixture = fixtures[variant]
        for field, expected_id in expected_ids.items():
            assert _fixture_value(fixture, field) == expected_id

    existing_ids = {
        getattr(thread, field)
        for thread in FIXTURE_THREADS
        for field in (
            "thread_id",
            "user_message_id",
            "assistant_message_id",
            "run_id",
        )
    }
    continuity_ids = {
        fixture_id
        for expected_ids in _CONTINUITY_IDS.values()
        for fixture_id in expected_ids.values()
    }
    assert len(continuity_ids) == 12
    assert existing_ids.isdisjoint(continuity_ids)

    beta = next(thread for thread in FIXTURE_THREADS if thread.label == "B")
    newest_history = max(
        thread.updated_at
        for thread in FIXTURE_THREADS
        if thread.label.startswith("HISTORY_")
    )
    closed_updated_at = _fixture_value(fixtures["closed"], "updated_at")
    open_updated_at = _fixture_value(fixtures["open"], "updated_at")
    assert beta.updated_at > closed_updated_at > open_updated_at > newest_history


def test_continuity_fixtures_have_completed_context_and_scrollable_sources() -> None:
    for fixture in _continuity_fixtures().values():
        assert _fixture_value(fixture, "completed_question")
        assert _fixture_value(fixture, "answer")
        assert _fixture_value(fixture, "active_question")
        assert _fixture_value(fixture, "missing_aspects")
        assert len(_fixture_value(fixture, "sources")) >= 8


@pytest.mark.asyncio
async def test_seed_inserts_completed_and_running_continuity_turns() -> None:
    owner_result = MagicMock()
    owner_result.scalar_one_or_none.return_value = seed_script._E2E_USER_ID
    execute = AsyncMock(return_value=owner_result)
    connection = SimpleNamespace(execute=execute)

    await seed_script._seed(connection)

    rows = _batched_rows(execute)
    rows_by_id = {row["id"]: row for row in rows if isinstance(row.get("id"), UUID)}
    for fixture in _continuity_fixtures().values():
        thread_id = _fixture_value(fixture, "thread_id")
        completed_user_id = _fixture_value(fixture, "completed_user_message_id")
        assistant_id = _fixture_value(fixture, "assistant_message_id")
        active_user_id = _fixture_value(fixture, "active_user_message_id")
        completed_run_id = _fixture_value(fixture, "completed_run_id")
        active_run_id = _fixture_value(fixture, "active_run_id")

        assert rows_by_id[thread_id]["updated_at"] == _fixture_value(
            fixture, "updated_at"
        )
        assert (
            rows_by_id[completed_user_id]["seq"],
            rows_by_id[completed_user_id]["role"],
        ) == (
            1,
            "user",
        )
        assert rows_by_id[completed_user_id]["content"] == _fixture_value(
            fixture, "completed_question"
        )
        assert (rows_by_id[assistant_id]["seq"], rows_by_id[assistant_id]["role"]) == (
            2,
            "assistant",
        )
        assert rows_by_id[assistant_id]["missing_aspects"] == list(
            _fixture_value(fixture, "missing_aspects")
        )
        assert (
            rows_by_id[active_user_id]["seq"],
            rows_by_id[active_user_id]["role"],
        ) == (
            3,
            "user",
        )
        assert rows_by_id[active_user_id]["content"] == _fixture_value(
            fixture, "active_question"
        )

        completed_run = rows_by_id[completed_run_id]
        assert completed_run["thread_id"] == thread_id
        assert completed_run["user_message_id"] == completed_user_id
        assert completed_run["assistant_message_id"] == assistant_id
        assert completed_run["status"] == "completed"

        active_run = rows_by_id[active_run_id]
        assert active_run["thread_id"] == thread_id
        assert active_run["user_message_id"] == active_user_id
        assert active_run["assistant_message_id"] is None
        assert active_run["status"] == "running"
        assert active_run["progress_stage"] == "synthesizing"
        assert active_run["error_code"] is None
        assert active_run["completed_at"] is None
        assert active_run["attempt_epoch"] == 1

        source_rows = [row for row in rows if row.get("message_id") == assistant_id]
        assert len(source_rows) == len(_fixture_value(fixture, "sources"))


@pytest.mark.asyncio
async def test_cleanup_is_limited_to_fixture_and_continuity_threads() -> None:
    expected_thread_ids = {
        *(thread.thread_id for thread in FIXTURE_THREADS),
        *(expected_ids["thread_id"] for expected_ids in _CONTINUITY_IDS.values()),
    }
    assert len(expected_thread_ids) == 22
    assert set(seed_script._THREAD_IDS) == expected_thread_ids

    execute = AsyncMock()
    await seed_script._cleanup(SimpleNamespace(execute=execute))

    statement = execute.await_args.args[0]
    bound_collections = [
        value
        for value in statement.compile().params.values()
        if isinstance(value, (list, tuple, set))
    ]
    assert len(bound_collections) == 1
    assert set(bound_collections[0]) == expected_thread_ids


def test_cli_parser_accepts_only_fixed_commands_and_variants() -> None:
    parser = seed_script.build_parser()
    accepted = {
        ("seed",): ("seed", None),
        ("cleanup",): ("cleanup", None),
        ("reset", "closed"): ("reset", "closed"),
        ("reset", "open"): ("reset", "open"),
        ("fail", "closed"): ("fail", "closed"),
        ("fail", "open"): ("fail", "open"),
    }
    for argv, expected in accepted.items():
        args = parser.parse_args(argv)
        assert (args.command, getattr(args, "variant", None)) == expected

    rejected = (
        ("unknown",),
        ("reset",),
        ("fail",),
        ("reset", "other"),
        ("fail", "00000000-0000-4000-a000-00000000d4f2"),
        ("seed", "closed"),
        ("cleanup", "open"),
        ("DROP TABLE agent_runs",),
    )
    for argv in rejected:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_production_guard_exits_before_database_access() -> None:
    with pytest.raises(SystemExit) as exc_info:
        guard_production("production")

    assert exc_info.value.code == 2


def test_every_cli_command_runs_production_guard_before_async_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_run = MagicMock()
    guard = MagicMock(side_effect=SystemExit(2))
    monkeypatch.setattr(seed_script.asyncio, "run", async_run)
    monkeypatch.setattr(seed_script, "guard_production", guard)
    monkeypatch.setenv("ENV", "production")

    for argv in (
        ("seed",),
        ("cleanup",),
        ("reset", "closed"),
        ("reset", "open"),
        ("fail", "closed"),
        ("fail", "open"),
    ):
        monkeypatch.setattr(seed_script.sys, "argv", ["seed_e2e_research.py", *argv])
        with pytest.raises(SystemExit) as exc_info:
            seed_script.main()
        assert exc_info.value.code == 2
        guard.assert_called_once_with("production")
        guard.reset_mock()

    async_run.assert_not_called()


@pytest.mark.asyncio
async def test_run_dispatches_each_fixed_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace()

    class BeginContext:
        async def __aenter__(self) -> SimpleNamespace:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    engine = SimpleNamespace(
        begin=lambda: BeginContext(),
        dispose=AsyncMock(),
    )
    seed = AsyncMock()
    cleanup = AsyncMock()
    reset = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(
        seed_script, "create_app_engine", lambda *_args, **_kwargs: engine
    )
    monkeypatch.setattr(seed_script, "_seed", seed)
    monkeypatch.setattr(seed_script, "_cleanup", cleanup)
    monkeypatch.setattr(seed_script, "_reset_continuity_run", reset)
    monkeypatch.setattr(seed_script, "_fail_continuity_run", fail)

    await seed_script.run("seed")
    await seed_script.run("cleanup")
    await seed_script.run("reset", "closed")
    await seed_script.run("fail", "open")

    seed.assert_awaited_once_with(connection)
    cleanup.assert_awaited_once_with(connection)
    assert connection in (*reset.await_args.args, *reset.await_args.kwargs.values())
    assert "closed" in (*reset.await_args.args, *reset.await_args.kwargs.values())
    assert connection in (*fail.await_args.args, *fail.await_args.kwargs.values())
    assert "open" in (*fail.await_args.args, *fail.await_args.kwargs.values())
    assert engine.dispose.await_count == 4


def _compiled_update(execute: AsyncMock) -> tuple[str, dict[str, Any]]:
    statement = execute.await_args.args[0]
    return str(statement), statement.compile().params


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ("closed", "open"))
async def test_reset_restores_only_the_variant_active_run(
    variant: str,
) -> None:
    reset_at = dt.datetime(2026, 7, 16, 12, 34, 56, tzinfo=dt.UTC)
    execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))

    await seed_script._reset_continuity_run(
        SimpleNamespace(execute=execute), variant, reset_at
    )

    sql, params = _compiled_update(execute)
    assert "UPDATE agent_runs" in sql
    assert "WHERE agent_runs.id" in sql
    assert _CONTINUITY_IDS[variant]["active_run_id"] in params.values()
    assert not {
        ids["active_run_id"]
        for other_variant, ids in _CONTINUITY_IDS.items()
        if other_variant != variant
    }.intersection(params.values())
    assert {
        "status": "running",
        "progress_stage": "synthesizing",
        "error_code": None,
        "assistant_message_id": None,
        "completed_at": None,
        "attempt_epoch": 1,
        "started_at": reset_at,
    }.items() <= params.items()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ("closed", "open"))
async def test_fail_transitions_only_a_running_variant_active_run(
    variant: str,
) -> None:
    failed_at = dt.datetime(2026, 7, 16, 12, 45, tzinfo=dt.UTC)
    execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))

    await seed_script._fail_continuity_run(
        SimpleNamespace(execute=execute), variant, failed_at
    )

    sql, params = _compiled_update(execute)
    assert "UPDATE agent_runs" in sql
    assert "WHERE agent_runs.id" in sql
    assert "agent_runs.status" in sql
    assert _CONTINUITY_IDS[variant]["active_run_id"] in params.values()
    assert "running" in params.values()
    assert {
        "status": "failed",
        "error_code": "internal_error",
        "completed_at": failed_at,
    }.items() <= params.items()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_name,variant,rowcount",
    (
        ("_reset_continuity_run", "closed", 0),
        ("_reset_continuity_run", "open", 2),
        ("_fail_continuity_run", "closed", 2),
        ("_fail_continuity_run", "open", 0),
    ),
)
async def test_continuity_mutations_require_exactly_one_updated_row(
    operation_name: str,
    variant: str,
    rowcount: int,
) -> None:
    execute = AsyncMock(return_value=SimpleNamespace(rowcount=rowcount))
    operation = getattr(seed_script, operation_name)

    with pytest.raises(RuntimeError):
        await operation(
            SimpleNamespace(execute=execute),
            variant,
            dt.datetime.now(dt.UTC),
        )
