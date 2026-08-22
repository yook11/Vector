"""External search期間解決の運用観測契約。"""

from __future__ import annotations

from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

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
                    "log_level": "warning",
                }
            ],
            id="failed",
        ),
    ],
)
def test_time_filter_resolution_observation_records_one_closed_metric_and_warning(
    capfire: CaptureLogfire,
    result: str,
    reason: str,
    expected_warning: list[dict[str, object]],
) -> None:
    with capture_logs() as logs:
        observability.observe_time_filter_resolution(
            result=result,
            reason=reason,
        )
    metrics = collected_metrics(capfire)
    warnings = [
        entry
        for entry in logs
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


@pytest.mark.parametrize("failing_sink", ["metric", "warning"])
def test_time_filter_resolution_observation_isolates_metric_and_warning_sink_failures(
    monkeypatch: pytest.MonkeyPatch,
    failing_sink: str,
) -> None:
    attempts: list[str] = []

    def record_time_filter_resolution(*, result: str, reason: str) -> None:
        attempts.append("metric")
        if failing_sink == "metric":
            raise RuntimeError("metric sink unavailable")

    def warning(event: str, **_kwargs: object) -> None:
        assert event == "external_search_time_filter_failed"
        attempts.append("warning")
        if failing_sink == "warning":
            raise RuntimeError("warning sink unavailable")

    monkeypatch.setattr(
        observability,
        "record_time_filter_resolution",
        record_time_filter_resolution,
    )
    monkeypatch.setattr(observability.logger, "warning", warning)

    observability.observe_time_filter_resolution(
        result="failed",
        reason="future_calendar_month",
    )

    assert attempts == ["metric", "warning"]
