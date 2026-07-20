"""User daily quota metric contracts."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from types import ModuleType
from typing import Any, cast

import pytest
from logfire.testing import CaptureLogfire

from tests.logfire._metric_helpers import collected_metrics

_ADMISSIONS_METRIC = "agent_user_daily_quota_admissions_total"
_RELEASES_METRIC = "agent_user_daily_quota_releases_total"
_STALE_RESERVATIONS_METRIC = "agent_user_daily_quota_stale_reservations_total"


def _daily_quota_observability_module() -> ModuleType:
    """未実装 module を collection error ではなく契約 failure にする。"""
    try:
        return import_module("app.agent.runs.daily_quota.observability")
    except ModuleNotFoundError as exc:
        if exc.name in {
            "app.agent.runs.daily_quota",
            "app.agent.runs.daily_quota.observability",
        }:
            pytest.fail("app.agent.runs.daily_quota.observability is not implemented")
        raise


def _recorder(module: ModuleType, name: str) -> Callable[..., None]:
    recorder = getattr(module, name, None)
    assert callable(recorder), f"{name} is not implemented"
    return cast(Callable[..., None], recorder)


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
    module = _daily_quota_observability_module()
    admission = _recorder(module, "record_daily_quota_admission")
    release = _recorder(module, "record_daily_quota_release")
    stale = _recorder(module, "record_daily_quota_stale_reservation")

    for result in ("accepted", "rejected"):
        admission(result=result)
    for result in ("released", "not_eligible", "inconsistent"):
        release(result=result)
    for previous_status in ("queued", "running"):
        stale(previous_status=previous_status)

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
