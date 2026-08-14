"""User daily quota metric contracts."""

from __future__ import annotations

from typing import Any

from logfire.testing import CaptureLogfire

from app.agent.runs.daily_quota.observability import (
    record_daily_quota_admission,
    record_daily_quota_release,
    record_daily_quota_stale_reservation,
)
from tests.logfire._metric_helpers import collected_metrics

_ADMISSIONS_METRIC = "agent_user_daily_quota_admissions_total"
_RELEASES_METRIC = "agent_user_daily_quota_releases_total"
_STALE_RESERVATIONS_METRIC = "agent_user_daily_quota_stale_reservations_total"


def _metric_points(
    metrics: list[dict[str, Any]],
    name: str,
) -> list[dict[str, Any]]:
    metric = next(
        (item for item in metrics if item["name"] == name),
        None,
    )
    if metric is None:
        return []
    return list(metric["data"]["data_points"])


def test_quota_recorders_emit_fixed_metrics_with_only_contract_attributes(
    capfire: CaptureLogfire,
) -> None:
    """利用枠 metric は低cardinalityな結果分類だけを持つ。"""
    for result in ("accepted", "rejected"):
        record_daily_quota_admission(result=result)
    for result in ("released", "not_eligible", "inconsistent"):
        record_daily_quota_release(result=result)
    for previous_status in ("queued", "running"):
        record_daily_quota_stale_reservation(previous_status=previous_status)

    metrics = collected_metrics(capfire)
    assert {
        (point["value"], frozenset(point.get("attributes", {}).items()))
        for point in _metric_points(metrics, _ADMISSIONS_METRIC)
    } == {
        (1, frozenset({("result", "accepted")})),
        (1, frozenset({("result", "rejected")})),
    }
    assert {
        (point["value"], frozenset(point.get("attributes", {}).items()))
        for point in _metric_points(metrics, _RELEASES_METRIC)
    } == {
        (1, frozenset({("result", "released")})),
        (1, frozenset({("result", "not_eligible")})),
        (1, frozenset({("result", "inconsistent")})),
    }
    assert {
        (point["value"], frozenset(point.get("attributes", {}).items()))
        for point in _metric_points(metrics, _STALE_RESERVATIONS_METRIC)
    } == {
        (1, frozenset({("previous_status", "queued")})),
        (1, frozenset({("previous_status", "running")})),
    }


def test_zero_count_release_and_stale_reservation_emit_no_data_point(
    capfire: CaptureLogfire,
) -> None:
    """count=0 は「対象なし」であり、値 0 の data point も作らない。"""
    record_daily_quota_release(result="released", count=0)
    record_daily_quota_stale_reservation(previous_status="queued", count=0)

    metrics = collected_metrics(capfire)
    assert _metric_points(metrics, _RELEASES_METRIC) == []
    assert _metric_points(metrics, _STALE_RESERVATIONS_METRIC) == []
