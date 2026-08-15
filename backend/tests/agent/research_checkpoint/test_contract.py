"""ResearchCheckpoint / ResearchTaskRecord の永続契約テスト。

上限はすべて仕様正本(RESEARCH_GOAL_MAX_CHARS 等)を import して境界値を
生成する。値をテストへ複製しないことで、正本の変更にテストが追随する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

import app.agent.contract as shared_contract
from app.agent.contract import (
    ANSWER_EVIDENCE_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
)
from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
    MISSING_ITEM_MAX_CHARS,
)
from app.agent.planning.contract import RESEARCH_GOAL_MAX_CHARS, RESEARCH_TASK_LIMIT
from app.agent.research_checkpoint.contract import (
    ResearchCheckpoint,
    ResearchTaskRecord,
)

_AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def test_legacy_import_sites_re_export_the_same_shared_contract_objects() -> None:
    """各工程contractのre-exportは複製ではなく、app.agent.contractと同一objectである。"""
    assert (
        ResearchCheckpoint is shared_contract.ResearchCheckpoint,
        ResearchTaskRecord is shared_contract.ResearchTaskRecord,
        RESEARCH_GOAL_MAX_CHARS is shared_contract.RESEARCH_GOAL_MAX_CHARS,
        RESEARCH_TASK_LIMIT is shared_contract.RESEARCH_TASK_LIMIT,
        EXTERNAL_TASK_QUERY_LIMIT is shared_contract.EXTERNAL_TASK_QUERY_LIMIT,
        EXTERNAL_QUERY_MAX_CHARS is shared_contract.EXTERNAL_QUERY_MAX_CHARS,
        EVIDENCE_CLAIM_MAX_CHARS is shared_contract.EVIDENCE_CLAIM_MAX_CHARS,
        MISSING_ITEM_MAX_CHARS is shared_contract.MISSING_ITEM_MAX_CHARS,
        ANSWER_EVIDENCE_LIMIT is shared_contract.ANSWER_EVIDENCE_LIMIT,
        EVIDENCE_REVIEW_MISSING_LIMIT is shared_contract.EVIDENCE_REVIEW_MISSING_LIMIT,
    ) == (True,) * 10


def _task_record(**overrides: object) -> ResearchTaskRecord:
    values: dict[str, object] = {
        "research_goal": "NVIDIA の供給動向を確認する",
        "executed_queries": ("NVIDIA 供給網 2026",),
        "adopted_claims": ("claim",),
    }
    values.update(overrides)
    return ResearchTaskRecord(**values)


def _checkpoint(**overrides: object) -> ResearchCheckpoint:
    values: dict[str, object] = {
        "as_of": _AS_OF,
        "tasks": (_task_record(),),
        "unresolved_after_search": ("在庫水準は未確認",),
    }
    values.update(overrides)
    return ResearchCheckpoint(**values)


def test_checkpoint_round_trips_through_json_dump_and_validate() -> None:
    checkpoint = _checkpoint(
        tasks=(
            _task_record(research_goal="goal-A", executed_queries=("q-a",)),
            _task_record(research_goal="goal-B", executed_queries=("q-b", "q-c")),
        ),
        unresolved_after_search=("missing-1", "missing-2"),
    )

    restored = ResearchCheckpoint.model_validate(checkpoint.model_dump(mode="json"))

    assert restored == checkpoint
    dumped = checkpoint.model_dump(mode="json")
    assert set(dumped) == {
        "schema_version",
        "as_of",
        "tasks",
        "unresolved_after_search",
    }
    assert set(dumped["tasks"][0]) == {
        "research_goal",
        "executed_queries",
        "adopted_claims",
    }


def test_checkpoint_rejects_unknown_field() -> None:
    payload = _checkpoint().model_dump(mode="json")
    payload["unexpected_field"] = "x"

    with pytest.raises(ValidationError):
        ResearchCheckpoint.model_validate(payload)


def test_task_record_rejects_unknown_field() -> None:
    payload = _task_record().model_dump(mode="json")
    payload["unexpected_field"] = "x"

    with pytest.raises(ValidationError):
        ResearchTaskRecord.model_validate(payload)


def test_checkpoint_rejects_naive_as_of() -> None:
    with pytest.raises(ValidationError):
        _checkpoint(as_of=datetime(2026, 8, 3, 9, 0))


def test_checkpoint_accepts_timezone_aware_as_of() -> None:
    checkpoint = _checkpoint(as_of=_AS_OF)

    assert checkpoint.as_of == _AS_OF


def test_checkpoint_is_frozen() -> None:
    checkpoint = _checkpoint()

    with pytest.raises(ValidationError):
        checkpoint.as_of = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)


def test_task_record_is_frozen() -> None:
    task_record = _task_record()

    with pytest.raises(ValidationError):
        task_record.research_goal = "changed"


def test_task_record_rejects_empty_executed_queries() -> None:
    with pytest.raises(ValidationError):
        _task_record(executed_queries=())


def test_checkpoint_rejects_empty_tasks() -> None:
    with pytest.raises(ValidationError):
        _checkpoint(tasks=())


def test_research_goal_accepts_exactly_the_max_char_limit() -> None:
    goal = "あ" * RESEARCH_GOAL_MAX_CHARS

    task_record = _task_record(research_goal=goal)

    assert task_record.research_goal == goal


def test_research_goal_rejects_one_char_over_the_max_limit() -> None:
    with pytest.raises(ValidationError):
        _task_record(research_goal="あ" * (RESEARCH_GOAL_MAX_CHARS + 1))


def test_executed_queries_accepts_exactly_the_task_query_limit() -> None:
    queries = tuple(f"query-{i}" for i in range(EXTERNAL_TASK_QUERY_LIMIT))

    task_record = _task_record(executed_queries=queries)

    assert task_record.executed_queries == queries


def test_executed_queries_rejects_one_more_than_the_task_query_limit() -> None:
    queries = tuple(f"query-{i}" for i in range(EXTERNAL_TASK_QUERY_LIMIT + 1))

    with pytest.raises(ValidationError):
        _task_record(executed_queries=queries)


def test_executed_query_accepts_exactly_the_max_char_limit() -> None:
    query = "q" * EXTERNAL_QUERY_MAX_CHARS

    task_record = _task_record(executed_queries=(query,))

    assert task_record.executed_queries == (query,)


def test_executed_query_rejects_one_char_over_the_max_limit() -> None:
    with pytest.raises(ValidationError):
        _task_record(executed_queries=("q" * (EXTERNAL_QUERY_MAX_CHARS + 1),))


def test_checkpoint_accepts_adopted_claims_total_at_exactly_the_limit() -> None:
    """adopted_claimsの上限はtask個別ではなくCheckpoint全task合計で境界になる。"""
    first_task_claim_count = ANSWER_EVIDENCE_LIMIT // 2
    second_task_claim_count = ANSWER_EVIDENCE_LIMIT - first_task_claim_count
    tasks = (
        _task_record(
            research_goal="goal-a",
            adopted_claims=tuple(f"claim-a-{i}" for i in range(first_task_claim_count)),
        ),
        _task_record(
            research_goal="goal-b",
            adopted_claims=tuple(
                f"claim-b-{i}" for i in range(second_task_claim_count)
            ),
        ),
    )

    checkpoint = _checkpoint(tasks=tasks)

    assert (
        sum(len(task.adopted_claims) for task in checkpoint.tasks)
        == ANSWER_EVIDENCE_LIMIT
    )


def test_checkpoint_rejects_adopted_claims_total_across_tasks_over_the_limit() -> None:
    """1 taskだけでは上限内でも、複数taskへ分散した合計が超過すれば拒否される。"""
    first_task_claim_count = ANSWER_EVIDENCE_LIMIT // 2
    second_task_claim_count = ANSWER_EVIDENCE_LIMIT - first_task_claim_count + 1
    tasks = (
        _task_record(
            research_goal="goal-a",
            adopted_claims=tuple(f"claim-a-{i}" for i in range(first_task_claim_count)),
        ),
        _task_record(
            research_goal="goal-b",
            adopted_claims=tuple(
                f"claim-b-{i}" for i in range(second_task_claim_count)
            ),
        ),
    )

    with pytest.raises(ValidationError):
        _checkpoint(tasks=tasks)


def test_adopted_claim_accepts_exactly_the_max_char_limit() -> None:
    claim = "c" * EVIDENCE_CLAIM_MAX_CHARS

    task_record = _task_record(adopted_claims=(claim,))

    assert task_record.adopted_claims == (claim,)


def test_adopted_claim_rejects_one_char_over_the_max_limit() -> None:
    with pytest.raises(ValidationError):
        _task_record(adopted_claims=("c" * (EVIDENCE_CLAIM_MAX_CHARS + 1),))


def test_tasks_accept_exactly_the_research_task_limit() -> None:
    tasks = tuple(
        _task_record(research_goal=f"goal-{i}") for i in range(RESEARCH_TASK_LIMIT)
    )

    checkpoint = _checkpoint(tasks=tasks)

    assert checkpoint.tasks == tasks


def test_tasks_reject_one_more_than_the_research_task_limit() -> None:
    tasks = tuple(
        _task_record(research_goal=f"goal-{i}") for i in range(RESEARCH_TASK_LIMIT + 1)
    )

    with pytest.raises(ValidationError):
        _checkpoint(tasks=tasks)


def test_unresolved_after_search_accepts_exactly_the_missing_limit() -> None:
    items = tuple(f"missing-{i}" for i in range(EVIDENCE_REVIEW_MISSING_LIMIT))

    checkpoint = _checkpoint(unresolved_after_search=items)

    assert checkpoint.unresolved_after_search == items


def test_unresolved_after_search_rejects_one_more_than_the_missing_limit() -> None:
    items = tuple(f"missing-{i}" for i in range(EVIDENCE_REVIEW_MISSING_LIMIT + 1))

    with pytest.raises(ValidationError):
        _checkpoint(unresolved_after_search=items)


def test_unresolved_item_accepts_exactly_the_max_char_limit() -> None:
    item = "m" * MISSING_ITEM_MAX_CHARS

    checkpoint = _checkpoint(unresolved_after_search=(item,))

    assert checkpoint.unresolved_after_search == (item,)


def test_unresolved_item_rejects_one_char_over_the_max_limit() -> None:
    with pytest.raises(ValidationError):
        _checkpoint(unresolved_after_search=("m" * (MISSING_ITEM_MAX_CHARS + 1),))
