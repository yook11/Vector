"""append_run_record() の積み上げ規則。

台帳に上限は無く、Run ごとの記録は落とさず末尾へ積む。整理はこの関数では
書き直さないため、前回の値をそのまま引き継ぐ。
"""

from __future__ import annotations

from datetime import UTC, datetime

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
            ResearchTaskRecord(research_goal=research_goal, executed_queries=("q",)),
        ),
    )


def test_first_record_starts_a_handoff_with_nothing_organized_yet() -> None:
    """最初のRunでは整理する対象が無く、整理3本は空のまま台帳だけが立つ。"""
    record = _record(1, research_goal="goal-1")

    handoff = append_run_record(previous=None, record=record)

    assert (handoff.runs, handoff.updated_at) == ((record,), record.as_of)
    assert (
        handoff.collected_overview,
        handoff.unresolved_points,
        handoff.next_search_guidance,
    ) == ("", "", "")


def test_later_records_are_appended_in_execution_order_without_dropping() -> None:
    """上限が無いため、古い記録は Run を重ねても落ちない。"""
    records = [_record(day, research_goal=f"goal-{day}") for day in (1, 2, 3, 4)]

    handoff: ResearchHandoff | None = None
    for record in records:
        handoff = append_run_record(previous=handoff, record=record)

    assert handoff is not None
    assert list(handoff.runs) == records
    assert handoff.updated_at == records[-1].as_of


def test_appending_carries_the_previous_organized_text_forward() -> None:
    """台帳を積む時点では整理を書き直さない。整理工程が失敗しても前回値が残る。"""
    previous = ResearchHandoff(
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        runs=(_record(1, research_goal="goal-1"),),
        collected_overview="Blackwell の供給記事が集まっている",
        unresolved_points="在庫水準は確認できていない",
        next_search_guidance="一次情報を優先する",
    )

    handoff = append_run_record(
        previous=previous,
        record=_record(2, research_goal="goal-2"),
    )

    assert (
        handoff.collected_overview,
        handoff.unresolved_points,
        handoff.next_search_guidance,
    ) == (
        previous.collected_overview,
        previous.unresolved_points,
        previous.next_search_guidance,
    )
