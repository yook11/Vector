"""External search outcome の永続契約(D4-S2)。

`ResearchTaskReport` は evidence_collection 側の contract へ移設された
(正本: tests/agent/evidence_collection/test_contract.py)。ここには
date filter / tool input など query-collection に閉じた契約と、
trim された `ExternalSearchOutcome`(evidence + agent counts +
deduplicated_evidence_count のみ)の契約だけを残す。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection.external_search import (
    ExternalQueryGenerationInput,
    ExternalSearchDateFilter,
    ExternalSearchEvidence,
    ExternalSearchOutcome,
)
from app.agent.planning.contract import TargetTimeWindow


def _unsafe_evidence(*, task_index: int, source_ref: str) -> ExternalSearchEvidence:
    return ExternalSearchEvidence.model_construct(
        source_ref=source_ref,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        url="https://example.com/evidence",
        title="evidence",
    )


def test_external_search_date_filter_is_a_frozen_half_open_value_object() -> None:
    date_filter = ExternalSearchDateFilter(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )

    assert (date_filter.start_date, date_filter.end_date) == (
        date(2026, 6, 1),
        date(2026, 6, 2),
    )


def test_external_search_date_filter_is_frozen() -> None:
    date_filter = ExternalSearchDateFilter(
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
    with pytest.raises(ValidationError):
        ExternalSearchDateFilter(start_date=start_date, end_date=end_date)


def test_external_search_date_filter_rejects_start_that_cannot_expand_one_day() -> None:
    with pytest.raises(ValidationError):
        ExternalSearchDateFilter(
            start_date=date.min,
            end_date=date.min + timedelta(days=1),
        )


def test_external_query_generation_input_uses_typed_time_window() -> None:
    hints = get_type_hints(ExternalQueryGenerationInput)

    assert hints["target_time_window"] == TargetTimeWindow | None


def test_outcome_rejects_duplicate_source_ref() -> None:
    """D4-S2: ExternalSearchOutcome は evidence + agent counts +

    deduplicated_evidence_count のみを保持する(task_reports/tasksは
    ReviewedEvidence側へ移設され、正本は
    tests/agent/evidence_review/test_reviewed_evidence.py)。
    ここに残るのはevidence自身のsource_ref一意性という自己完結した契約のみ。
    """
    with pytest.raises(ValidationError):
        ExternalSearchOutcome(
            evidence=[
                _unsafe_evidence(task_index=0, source_ref="0-0"),
                _unsafe_evidence(task_index=1, source_ref="0-0"),
            ],
            effective_agent_count=2,
        )


def test_outcome_has_no_task_reports_or_tasks_field() -> None:
    """保証するテスト条件 1(旧語彙不在)。task_reports/tasksはtrimされる。"""
    outcome = ExternalSearchOutcome(evidence=[], effective_agent_count=1)

    assert not hasattr(outcome, "task_reports")
    assert not hasattr(outcome, "tasks")
