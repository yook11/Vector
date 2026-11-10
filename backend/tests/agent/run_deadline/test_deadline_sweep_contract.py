"""deadline sweepのquota観測の契約。"""

from __future__ import annotations

import pytest

import app.agent.running.daily_quota.observability as quota_observability


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
