"""External search outcome の永続契約。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError

import app.agent.evidence_collection.external_search as external_search
from app.agent.planning import contract as planning_contract
from app.agent.planning.contract import ExternalResearchTask


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=goal)


def _report(*, task_index: int, evidence_count: int = 0) -> Any:
    return external_search.ResearchTaskReport(
        task_index=task_index,
        collection_goal=f"task {task_index}",
        status="succeeded",
        evidence_count=evidence_count,
    )


def _time_filter_failed_report(**changes: object) -> Any:
    values: dict[str, object] = {
        "task_index": 0,
        "collection_goal": "指定期間の根拠を確認する",
        "status": "time_filter_failed",
        "time_filter_failure_reason": "future_calendar_month",
    }
    values.update(changes)
    return external_search.ResearchTaskReport(**values)


def _unsafe_evidence(*, task_index: int, source_ref: str) -> Any:
    return external_search.ExternalSearchEvidence.model_construct(
        source_ref=source_ref,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        url="https://example.com/evidence",
        title="evidence",
    )


def _required_external_search_attribute(name: str) -> Any:
    value = getattr(external_search, name, None)
    if value is None:
        pytest.fail(f"external search contract must define {name}")
    return value


def test_external_search_date_filter_is_a_frozen_half_open_value_object() -> None:
    date_filter_type = _required_external_search_attribute("ExternalSearchDateFilter")
    date_filter = date_filter_type(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )

    assert (date_filter.start_date, date_filter.end_date) == (
        date(2026, 6, 1),
        date(2026, 6, 2),
    )


def test_external_search_date_filter_is_frozen() -> None:
    date_filter_type = _required_external_search_attribute("ExternalSearchDateFilter")
    date_filter = date_filter_type(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )

    with pytest.raises(ValidationError):
        date_filter.end_date = date(2026, 6, 3)


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        pytest.param(date(2026, 6, 1), date(2026, 6, 1), id="same-day"),
        pytest.param(date(2026, 6, 2), date(2026, 6, 1), id="reverse-order"),
    ],
)
def test_external_search_date_filter_rejects_non_half_open_ranges(
    start_date: date,
    end_date: date,
) -> None:
    date_filter_type = _required_external_search_attribute("ExternalSearchDateFilter")

    with pytest.raises(ValidationError):
        date_filter_type(start_date=start_date, end_date=end_date)


def test_external_search_date_filter_rejects_start_that_cannot_expand_one_day() -> None:
    date_filter_type = _required_external_search_attribute("ExternalSearchDateFilter")

    with pytest.raises(ValidationError):
        date_filter_type(
            start_date=date.min,
            end_date=date.min + timedelta(days=1),
        )


def test_time_filter_failed_report_keeps_only_closed_diagnostics() -> None:
    report = _time_filter_failed_report()

    assert report.model_dump() == {
        "task_index": 0,
        "collection_goal": "指定期間の根拠を確認する",
        "generated_queries": [],
        "status": "time_filter_failed",
        "time_filter_failure_reason": "future_calendar_month",
        "provider_failed_query_count": 0,
        "candidate_count": 0,
        "evidence_count": 0,
        "dropped_selection_count": 0,
        "selector_failure_reason": None,
        "missing": [],
    }


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"time_filter_failure_reason": None}, id="missing-reason"),
        pytest.param({"generated_queries": ["raw query"]}, id="generated-query"),
        pytest.param({"provider_failed_query_count": 1}, id="provider-failure"),
        pytest.param({"candidate_count": 1}, id="candidate"),
        pytest.param({"evidence_count": 1}, id="evidence"),
        pytest.param({"dropped_selection_count": 1}, id="dropped-selection"),
        pytest.param({"selector_failure_reason": "timeout"}, id="selector-reason"),
        pytest.param({"missing": ["表示用の不足理由"]}, id="missing"),
    ],
)
def test_time_filter_failed_report_rejects_non_closed_diagnostics(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _time_filter_failed_report(**changes)


@pytest.mark.parametrize(
    "status",
    [
        pytest.param("succeeded"),
        pytest.param("query_generation_failed"),
        pytest.param("provider_failed"),
        pytest.param("selector_failed"),
    ],
)
def test_non_time_filter_report_rejects_time_filter_failure_reason(status: str) -> None:
    with pytest.raises(ValidationError):
        external_search.ResearchTaskReport(
            task_index=0,
            collection_goal="通常の外部検索task",
            status=status,
            time_filter_failure_reason="future_date_range",
        )


def test_external_search_tool_input_carries_only_resolved_date_filter() -> None:
    date_filter_type = _required_external_search_attribute("ExternalSearchDateFilter")
    date_filter = date_filter_type(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )
    tool_input = external_search.ExternalSearchToolInput(
        query="NVIDIA 発表",
        limit=3,
        date_filter=date_filter,
    )

    assert tool_input.date_filter == date_filter


def test_external_query_generation_input_uses_typed_time_window() -> None:
    hints = get_type_hints(external_search.ExternalQueryGenerationInput)
    target_time_window_type = getattr(planning_contract, "TargetTimeWindow", None)
    if target_time_window_type is None:
        pytest.fail("planning contract must define TargetTimeWindow")

    assert hints["target_time_window"] == target_time_window_type | None


@pytest.mark.parametrize(
    "reports",
    [
        [_report(task_index=0)],
        [_report(task_index=0), _report(task_index=0)],
        [_report(task_index=0), _report(task_index=2)],
    ],
)
def test_outcome_rejects_report_count_duplicate_and_missing_task_indexes(
    reports: list[Any],
) -> None:
    with pytest.raises(ValidationError):
        external_search.ExternalSearchOutcome(
            tasks=[_task("first"), _task("second")],
            task_reports=reports,
            effective_agent_count=2,
        )


def test_outcome_rejects_evidence_outside_task_range_and_duplicate_source_ref() -> None:
    with pytest.raises(ValidationError):
        external_search.ExternalSearchOutcome(
            tasks=[_task("first")],
            evidence=[_unsafe_evidence(task_index=1, source_ref="external-1-0")],
            task_reports=[_report(task_index=0)],
            effective_agent_count=1,
        )

    with pytest.raises(ValidationError):
        external_search.ExternalSearchOutcome(
            tasks=[_task("first"), _task("second")],
            evidence=[
                _unsafe_evidence(task_index=0, source_ref="external-0-0"),
                _unsafe_evidence(task_index=1, source_ref="external-0-0"),
            ],
            task_reports=[_report(task_index=0), _report(task_index=1)],
            effective_agent_count=2,
        )


def test_outcome_rejects_evidence_count_that_cannot_be_explained_by_deduplication() -> (
    None
):
    with pytest.raises(ValidationError):
        external_search.ExternalSearchOutcome(
            tasks=[_task("first")],
            evidence=[_unsafe_evidence(task_index=0, source_ref="external-0-0")],
            task_reports=[_report(task_index=0, evidence_count=2)],
            deduplicated_evidence_count=0,
            effective_agent_count=1,
        )
