"""台帳(build_research_run_record / append_run_record)の契約テスト。

LLM呼び出しを追加しない決定的な組み立てであるため、期待値は入力(plan/
executed_queries_by_task)から導出し、production関数を呼んで作らない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.contract import ResearchRunRecord
from app.agent.planning.contract import ResearchTask, SearchPlan
from app.agent.research_handoff import ResearchHandoff, ResearchTaskRecord
from app.agent.research_handoff.ledger import (
    append_run_record,
    build_research_run_record,
)

_AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def _plan(*, goals: list[str]) -> SearchPlan:
    return SearchPlan(
        research_tasks=[
            ResearchTask(research_goal=goal, article_search_queries=["seed query"])
            for goal in goals
        ]
    )


def _build(
    *,
    plan: SearchPlan,
    executed_queries_by_task: dict[int, tuple[str, ...]],
    as_of: datetime = _AS_OF,
) -> ResearchRunRecord | None:
    return build_research_run_record(
        plan=plan,
        executed_queries_by_task=executed_queries_by_task,
        as_of=as_of,
    )


def test_tasks_are_recorded_in_plan_task_index_order_with_verbatim_queries() -> None:
    """plannerの重複判定はquery文字列そのものを読むため、加工せず順序ごと残す。"""
    plan = _plan(goals=["goal-A", "goal-B"])
    executed_queries_by_task = {0: ("q-a1",), 1: ("q-b1", "q-b2")}

    record = _build(plan=plan, executed_queries_by_task=executed_queries_by_task)

    assert record is not None
    assert [(task.research_goal, task.executed_queries) for task in record.tasks] == [
        ("goal-A", ("q-a1",)),
        ("goal-B", ("q-b1", "q-b2")),
    ]


def test_task_with_missing_executed_queries_entry_is_not_recorded() -> None:
    """外部検索へ到達しなかったtaskは、叩いたqueryが無いので台帳に残さない。"""
    plan = _plan(goals=["goal-A", "goal-B"])

    record = _build(plan=plan, executed_queries_by_task={1: ("q-b1",)})

    assert record is not None
    assert [task.research_goal for task in record.tasks] == ["goal-B"]


def test_task_with_empty_executed_queries_tuple_is_not_recorded() -> None:
    plan = _plan(goals=["goal-A", "goal-B"])

    record = _build(plan=plan, executed_queries_by_task={0: (), 1: ("q-b1",)})

    assert record is not None
    assert [task.research_goal for task in record.tasks] == ["goal-B"]


def test_zero_recordable_tasks_returns_none() -> None:
    """1本もqueryを叩けなかったRunは申し送りを触らない。"""
    plan = _plan(goals=["goal-A"])

    assert _build(plan=plan, executed_queries_by_task={0: ()}) is None


def test_as_of_is_carried_through_unchanged() -> None:
    """調査時点は鮮度の再確認かどうかの判断に使うため、丸めずに残す。"""
    plan = _plan(goals=["goal-A"])
    as_of = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    record = _build(
        plan=plan,
        executed_queries_by_task={0: ("q-a1",)},
        as_of=as_of,
    )

    assert record is not None
    assert record.as_of == as_of


def _dated_record(day: int, *, research_goal: str) -> ResearchRunRecord:
    return ResearchRunRecord(
        as_of=datetime(2026, 8, day, tzinfo=UTC),
        tasks=(
            ResearchTaskRecord(research_goal=research_goal, executed_queries=("q",)),
        ),
    )


def test_first_record_starts_a_handoff_with_nothing_organized_yet() -> None:
    """最初のRunでは整理する対象が無く、整理3本は空のまま台帳だけが立つ。"""
    record = _dated_record(1, research_goal="goal-1")

    handoff = append_run_record(previous=None, record=record)

    assert (handoff.runs, handoff.updated_at) == ((record,), record.as_of)
    assert (
        handoff.collected_overview,
        handoff.unresolved_points,
        handoff.next_search_guidance,
    ) == ("", "", "")


def test_later_records_are_appended_in_execution_order_without_dropping() -> None:
    """上限が無いため、古い記録は Run を重ねても落ちない。"""
    records = [_dated_record(day, research_goal=f"goal-{day}") for day in (1, 2, 3, 4)]

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
        runs=(_dated_record(1, research_goal="goal-1"),),
        collected_overview="Blackwell の供給記事が集まっている",
        unresolved_points="在庫水準は確認できていない",
        next_search_guidance="一次情報を優先する",
    )

    handoff = append_run_record(
        previous=previous,
        record=_dated_record(2, research_goal="goal-2"),
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
