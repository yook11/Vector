"""External search outcome の永続契約。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import app.agent.evidence_collection.external_search as external_search
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


def _unsafe_evidence(*, task_index: int, source_ref: str) -> Any:
    return external_search.ExternalSearchEvidence.model_construct(
        source_ref=source_ref,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        url="https://example.com/evidence",
        title="evidence",
    )


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
