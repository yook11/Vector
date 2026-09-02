"""deadline sweepの通知対象とquota観測の契約。"""

from __future__ import annotations

from uuid import UUID

import pytest

import app.agent.daily_quota.observability as quota_observability
from app.agent.run_deadline.contracts import DeadlineExceededRunningRun


def test_deadline_sweep_result_rejects_nonpositive_running_attempt_epoch() -> None:
    with pytest.raises(ValueError, match="positive attempt epoch"):
        DeadlineExceededRunningRun(
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
