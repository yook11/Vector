"""External search期間解決の運用観測契約。"""

from __future__ import annotations

from typing import Any

import pytest
from logfire.testing import CaptureLogfire

import app.agent.evidence_collection.external_search.observability as observability
from tests.logfire._metric_helpers import collected_metrics

_METRIC_NAME = "external_search_time_filter_resolution_total"


def _metric_points(metrics: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    metric = next((item for item in metrics if item["name"] == _METRIC_NAME), None)
    if metric is None:
        return []
    return [
        (int(point["value"]), point.get("attributes", {}))
        for point in metric["data"]["data_points"]
    ]


@pytest.mark.parametrize(
    ("result", "reason", "expected_warning"),
    [
        pytest.param("not_requested", "none", [], id="not-requested"),
        pytest.param("resolved", "none", [], id="resolved"),
        pytest.param(
            "failed",
            "future_date_range",
            [
                {
                    "reason": "future_date_range",
                    "event": "external_search_time_filter_failed",
                    "level": "warning",
                    "log_policy": "research",
                }
            ],
            id="failed",
        ),
    ],
)
def test_time_filter_resolution_observation_records_one_closed_metric_and_warning(
    capfire: CaptureLogfire,
    read_logs,
    result: str,
    reason: str,
    expected_warning: list[dict[str, object]],
) -> None:
    observability.observe_time_filter_resolution(
        result=result,
        reason=reason,
    )
    metrics = collected_metrics(capfire)
    warnings = [
        entry
        for entry in read_logs()
        if entry.get("event") == "external_search_time_filter_failed"
    ]

    assert (
        _metric_points(metrics),
        warnings,
    ) == (
        [(1, {"result": result, "reason": reason})],
        expected_warning,
    )


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        pytest.param("not_requested", "future_calendar_month"),
        pytest.param("resolved", "unsupported_explicit_window"),
        pytest.param("failed", "none"),
    ],
)
def test_time_filter_resolution_observation_rejects_inconsistent_closed_pairs(
    result: str,
    reason: str,
) -> None:
    with pytest.raises(ValueError):
        observability.observe_time_filter_resolution(
            result=result,
            reason=reason,
        )


def test_time_filter_resolution_metric_failure_keeps_warning(
    monkeypatch: pytest.MonkeyPatch,
    read_logs,
) -> None:
    """メトリクスの記録に失敗しても、期間解決の失敗のwarningは出る。"""

    def record_time_filter_resolution(*, result: str, reason: str) -> None:
        raise RuntimeError("metric sink unavailable")

    monkeypatch.setattr(
        observability,
        "record_time_filter_resolution",
        record_time_filter_resolution,
    )

    observability.observe_time_filter_resolution(
        result="failed",
        reason="future_calendar_month",
    )

    assert read_logs() == [
        {
            "reason": "future_calendar_month",
            "event": "external_search_time_filter_failed",
            "level": "warning",
            "log_policy": "research",
        }
    ]


def test_time_filter_resolution_log_failure_keeps_metric(
    monkeypatch: pytest.MonkeyPatch,
    broken_output,
) -> None:
    """ログの書き込みに失敗しても、メトリクスは記録され、呼び出し元へ例外は上がらない。"""
    attempts: list[str] = []

    def record_time_filter_resolution(*, result: str, reason: str) -> None:
        attempts.append("metric")

    monkeypatch.setattr(
        observability,
        "record_time_filter_resolution",
        record_time_filter_resolution,
    )

    observability.observe_time_filter_resolution(
        result="failed",
        reason="future_calendar_month",
    )

    assert attempts == ["metric"]
