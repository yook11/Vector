"""append_run_record() の積み上げ規則。

記録層に上限は無く、Run ごとの記録は落とさず末尾へ積む。判断層はこの工程では
生成されないため、前回の値をそのまま引き継ぐ。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
    append_run_record,
)


def _record(day: int, *, research_goal: str) -> ResearchRunRecord:
    return ResearchRunRecord(
        as_of=datetime(2026, 8, day, tzinfo=UTC),
        tasks=(
            ResearchTaskRecord(
                research_goal=research_goal,
                executed_queries=("q",),
                adopted_claims=(),
            ),
        ),
        unresolved_after_search=(),
    )


def test_first_record_starts_a_handoff_with_an_empty_judgement_layer() -> None:
    record = _record(1, research_goal="goal-1")

    handoff = append_run_record(previous=None, record=record)

    assert (
        handoff.runs,
        handoff.updated_at,
        handoff.standing_inquiry,
        handoff.next_directives,
    ) == ((record,), record.as_of, "", ())


def test_later_records_are_appended_in_execution_order_without_dropping() -> None:
    """上限が無いため、古い記録は Run を重ねても落ちない。"""
    records = [_record(day, research_goal=f"goal-{day}") for day in (1, 2, 3, 4)]

    handoff: ResearchHandoff | None = None
    for record in records:
        handoff = append_run_record(previous=handoff, record=record)

    assert handoff is not None
    assert list(handoff.runs) == records
    assert handoff.updated_at == records[-1].as_of


def test_appending_carries_the_previous_judgement_layer_forward() -> None:
    previous = ResearchHandoff(
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        standing_inquiry="Blackwell の投資判断",
        runs=(_record(1, research_goal="goal-1"),),
        next_directives=("一次情報を優先する",),
    )

    handoff = append_run_record(
        previous=previous,
        record=_record(2, research_goal="goal-2"),
    )

    assert (handoff.standing_inquiry, handoff.next_directives) == (
        previous.standing_inquiry,
        previous.next_directives,
    )


def test_handoff_rejects_an_empty_run_list_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ResearchHandoff(updated_at=datetime(2026, 8, 1, tzinfo=UTC), runs=())
    with pytest.raises(ValidationError):
        ResearchHandoff(
            updated_at=datetime(2026, 8, 1, tzinfo=UTC),
            runs=(_record(1, research_goal="goal-1"),),
            telemetry=object(),
        )


def test_handoff_rejects_a_naive_updated_at() -> None:
    with pytest.raises(ValidationError):
        ResearchHandoff(
            updated_at=datetime(2026, 8, 1),
            runs=(_record(1, research_goal="goal-1"),),
        )
