import pytest

from scripts.seed_e2e_research import FIXTURE_THREADS, guard_production


def test_fixture_has_three_threads_in_deterministic_descending_order() -> None:
    assert len(FIXTURE_THREADS) == 3
    assert [thread.label for thread in FIXTURE_THREADS] == ["A", "B", "C"]
    assert [thread.updated_at for thread in FIXTURE_THREADS] == sorted(
        (thread.updated_at for thread in FIXTURE_THREADS), reverse=True
    )


def test_production_guard_exits_before_database_access() -> None:
    with pytest.raises(SystemExit) as exc_info:
        guard_production("production")

    assert exc_info.value.code == 2
