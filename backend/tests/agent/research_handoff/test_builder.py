"""build_research_run_record() の決定的な詰め替え契約テスト。

LLM呼び出しを追加しない決定的builderであるため、期待値は入力(plan/
executed_queries_by_task)から導出し、production関数を呼んで作らない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.contract import ResearchRunRecord
from app.agent.planning.contract import ResearchTask, SearchPlan
from app.agent.research_handoff.builder import build_research_run_record

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
