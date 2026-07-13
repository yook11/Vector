from itertools import pairwise
from uuid import UUID

import pytest

from scripts.seed_e2e_research import FIXTURE_THREADS, guard_production


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


def test_production_guard_exits_before_database_access() -> None:
    with pytest.raises(SystemExit) as exc_info:
        guard_production("production")

    assert exc_info.value.code == 2
