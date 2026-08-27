"""ResearchHandoff の永続契約テスト。

上限はすべて仕様正本(RESEARCH_GOAL_MAX_CHARS 等)を import して境界値を
生成する。値をテストへ複製しないことで、正本の変更にテストが追随する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.contract import ORGANIZED_TEXT_MAX_CHARS
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
)
from app.agent.planning.contract import RESEARCH_GOAL_MAX_CHARS, RESEARCH_TASK_LIMIT
from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)

_AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)

_ORGANIZED_FIELDS = (
    "collected_overview",
    "unresolved_points",
    "next_search_guidance",
)


def _task_record(**overrides: object) -> ResearchTaskRecord:
    values: dict[str, object] = {
        "research_goal": "NVIDIA の供給動向を確認する",
        "executed_queries": ("NVIDIA 供給網 2026",),
    }
    values.update(overrides)
    return ResearchTaskRecord(**values)


def _run_record(**overrides: object) -> ResearchRunRecord:
    values: dict[str, object] = {"as_of": _AS_OF, "tasks": (_task_record(),)}
    values.update(overrides)
    return ResearchRunRecord(**values)


def _handoff(**overrides: object) -> ResearchHandoff:
    values: dict[str, object] = {"updated_at": _AS_OF, "runs": (_run_record(),)}
    values.update(overrides)
    return ResearchHandoff(**values)


def test_handoff_round_trips_through_json_dump_and_validate() -> None:
    """保存して読み戻したhandoffは、書いたときと同じものとして復元される。"""
    handoff = _handoff(
        runs=(
            _run_record(
                tasks=(
                    _task_record(research_goal="goal-A", executed_queries=("q-a",)),
                    _task_record(
                        research_goal="goal-B", executed_queries=("q-b", "q-c")
                    ),
                ),
            ),
        ),
        collected_overview="供給網の記事が3件集まった",
        unresolved_points="在庫水準は確認できていない",
        next_search_guidance="決算資料を直接あたるとよい",
    )

    restored = ResearchHandoff.model_validate(handoff.model_dump(mode="json"))

    assert restored == handoff


def test_handoff_rejects_a_run_list_that_is_empty() -> None:
    """台帳を1件も持たないhandoffは書かれない(触らないことでNoneと区別する)。"""
    with pytest.raises(ValidationError):
        _handoff(runs=())


def test_handoff_rejects_unknown_field() -> None:
    payload = _handoff().model_dump(mode="json")
    payload["unexpected_field"] = "x"

    with pytest.raises(ValidationError):
        ResearchHandoff.model_validate(payload)


def test_handoff_rejects_naive_updated_at() -> None:
    with pytest.raises(ValidationError):
        _handoff(updated_at=datetime(2026, 8, 3, 9, 0))


def test_run_record_rejects_naive_as_of() -> None:
    """調査時点が曖昧だと、鮮度の再確認かどうかを後段が判断できない。"""
    with pytest.raises(ValidationError):
        _run_record(as_of=datetime(2026, 8, 3, 9, 0))


def test_run_record_rejects_unknown_field() -> None:
    payload = _run_record().model_dump(mode="json")
    payload["unexpected_field"] = "x"

    with pytest.raises(ValidationError):
        ResearchRunRecord.model_validate(payload)


def test_task_record_rejects_unknown_field() -> None:
    payload = _task_record().model_dump(mode="json")
    payload["unexpected_field"] = "x"

    with pytest.raises(ValidationError):
        ResearchTaskRecord.model_validate(payload)


def test_handoff_is_frozen() -> None:
    handoff = _handoff()

    with pytest.raises(ValidationError):
        handoff.collected_overview = "changed"


def test_task_record_is_frozen() -> None:
    task_record = _task_record()

    with pytest.raises(ValidationError):
        task_record.research_goal = "changed"


def test_task_record_rejects_empty_executed_queries() -> None:
    """queryを1本も叩けなかったtaskは台帳に残さない。"""
    with pytest.raises(ValidationError):
        _task_record(executed_queries=())


def test_run_record_rejects_empty_tasks() -> None:
    with pytest.raises(ValidationError):
        _run_record(tasks=())


def test_research_goal_accepts_exactly_the_max_char_limit() -> None:
    goal = "あ" * RESEARCH_GOAL_MAX_CHARS

    assert _task_record(research_goal=goal).research_goal == goal


def test_research_goal_rejects_one_char_over_the_max_limit() -> None:
    with pytest.raises(ValidationError):
        _task_record(research_goal="あ" * (RESEARCH_GOAL_MAX_CHARS + 1))


def test_executed_queries_accept_exactly_the_task_query_limit() -> None:
    queries = tuple(f"query-{i}" for i in range(EXTERNAL_TASK_QUERY_LIMIT))

    assert _task_record(executed_queries=queries).executed_queries == queries


def test_executed_queries_reject_one_more_than_the_task_query_limit() -> None:
    queries = tuple(f"query-{i}" for i in range(EXTERNAL_TASK_QUERY_LIMIT + 1))

    with pytest.raises(ValidationError):
        _task_record(executed_queries=queries)


def test_executed_query_accepts_exactly_the_max_char_limit() -> None:
    query = "q" * EXTERNAL_QUERY_MAX_CHARS

    assert _task_record(executed_queries=(query,)).executed_queries == (query,)


def test_executed_query_rejects_one_char_over_the_max_limit() -> None:
    with pytest.raises(ValidationError):
        _task_record(executed_queries=("q" * (EXTERNAL_QUERY_MAX_CHARS + 1),))


def test_tasks_accept_exactly_the_research_task_limit() -> None:
    tasks = tuple(
        _task_record(research_goal=f"goal-{i}") for i in range(RESEARCH_TASK_LIMIT)
    )

    assert _run_record(tasks=tasks).tasks == tasks


def test_tasks_reject_one_more_than_the_research_task_limit() -> None:
    tasks = tuple(
        _task_record(research_goal=f"goal-{i}") for i in range(RESEARCH_TASK_LIMIT + 1)
    )

    with pytest.raises(ValidationError):
        _run_record(tasks=tasks)


@pytest.mark.parametrize("field", _ORGANIZED_FIELDS)
def test_organized_text_accepts_exactly_the_max_char_limit(field: str) -> None:
    text = "整" * ORGANIZED_TEXT_MAX_CHARS

    assert getattr(_handoff(**{field: text}), field) == text


@pytest.mark.parametrize("field", _ORGANIZED_FIELDS)
def test_organized_text_rejects_one_char_over_the_max_limit(field: str) -> None:
    """thread全体を1本へ畳んだ結果であり、Run数によらず一定に保つ。"""
    with pytest.raises(ValidationError):
        _handoff(**{field: "整" * (ORGANIZED_TEXT_MAX_CHARS + 1)})
