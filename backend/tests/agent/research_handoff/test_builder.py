"""build_research_run_record() の決定的な詰め替え契約テスト。

LLM呼び出しを追加しない決定的builderであるため、期待値は入力(plan/
executed_queries_by_task/evidence_run)から導出し、production関数を
呼んで作らない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.agent.evidence_review.answer_evidence import (
    AnswerEvidence,
    EvidenceRunCompleted,
    ExternalSearchEvidence,
    InternalArticleEvidence,
)
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


def _external_evidence(
    *, task_index: int, claim: str, option_index: int = 0
) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        option_index=option_index,
        task_index=task_index,
        claim=claim,
        why_selected="why",
        url="https://example.com/evidence",
        title="evidence",
    )


def _internal_evidence(
    *, task_index: int, claim: str, option_index: int = 0
) -> InternalArticleEvidence:
    return InternalArticleEvidence(
        option_index=option_index,
        task_index=task_index,
        claim=claim,
        why_selected="why",
        assessment_id=1001,
        curation_id=1,
        title="internal title",
        summary="internal summary",
        key_points=[],
        published_at=None,
    )


def _outcome(
    *,
    external_evidence: list[ExternalSearchEvidence] | None = None,
    internal_evidence: list[InternalArticleEvidence] | None = None,
    missing: list[str] | None = None,
) -> EvidenceRunCompleted:
    return EvidenceRunCompleted(
        answer_evidence=AnswerEvidence(
            internal_evidence=tuple(internal_evidence or []),
            external_evidence=tuple(external_evidence or []),
        ),
        review_missing=tuple(missing or []),
    )


def _build(
    *,
    plan: SearchPlan,
    executed_queries_by_task: dict[int, tuple[str, ...]],
    evidence_run: EvidenceRunCompleted,
    as_of: datetime = _AS_OF,
) -> Any:
    return build_research_run_record(
        plan=plan,
        executed_queries_by_task=executed_queries_by_task,
        evidence_run=evidence_run,
        as_of=as_of,
    )


def test_tasks_are_recorded_in_plan_task_index_order_with_verbatim_queries() -> None:
    plan = _plan(goals=["goal-A", "goal-B"])
    executed_queries_by_task = {0: ("q-a1",), 1: ("q-b1", "q-b2")}

    checkpoint = _build(
        plan=plan,
        executed_queries_by_task=executed_queries_by_task,
        evidence_run=_outcome(),
    )

    assert checkpoint is not None
    assert [task.research_goal for task in checkpoint.tasks] == ["goal-A", "goal-B"]
    assert checkpoint.tasks[0].executed_queries == executed_queries_by_task[0]
    assert checkpoint.tasks[1].executed_queries == executed_queries_by_task[1]


def test_adopted_claims_include_only_external_evidence_for_the_matching_task() -> None:
    plan = _plan(goals=["goal-A"])
    evidence_run = _outcome(
        external_evidence=[
            _external_evidence(task_index=0, claim="external claim", option_index=0)
        ],
        internal_evidence=[
            _internal_evidence(task_index=0, claim="internal claim", option_index=1)
        ],
    )

    checkpoint = _build(
        plan=plan,
        executed_queries_by_task={0: ("q-a",)},
        evidence_run=evidence_run,
    )

    assert checkpoint is not None
    assert checkpoint.tasks[0].adopted_claims == ("external claim",)


def test_checkpoint_rejects_evidence_for_an_unrecorded_task_index() -> None:
    plan = _plan(goals=["goal-A"])
    evidence_run = _outcome(
        external_evidence=[
            _external_evidence(task_index=1, claim="unrecorded task claim")
        ]
    )

    with pytest.raises(ValueError):
        _build(
            plan=plan,
            executed_queries_by_task={0: ("q-a",)},
            evidence_run=evidence_run,
        )


def test_task_with_no_matching_adopted_claim_is_recorded_with_an_empty_tuple() -> None:
    plan = _plan(goals=["goal-A"])

    checkpoint = _build(
        plan=plan,
        executed_queries_by_task={0: ("q-a",)},
        evidence_run=_outcome(),
    )

    assert checkpoint is not None
    assert len(checkpoint.tasks) == 1
    assert checkpoint.tasks[0].adopted_claims == ()


def test_task_with_missing_executed_queries_entry_is_not_recorded() -> None:
    plan = _plan(goals=["goal-A", "goal-B"])

    checkpoint = _build(
        plan=plan,
        executed_queries_by_task={1: ("q-b",)},
        evidence_run=_outcome(),
    )

    assert checkpoint is not None
    assert [task.research_goal for task in checkpoint.tasks] == ["goal-B"]


def test_task_with_empty_executed_queries_tuple_is_not_recorded() -> None:
    plan = _plan(goals=["goal-A", "goal-B"])

    checkpoint = _build(
        plan=plan,
        executed_queries_by_task={0: (), 1: ("q-b",)},
        evidence_run=_outcome(),
    )

    assert checkpoint is not None
    assert [task.research_goal for task in checkpoint.tasks] == ["goal-B"]


def test_zero_recordable_tasks_returns_none() -> None:
    plan = _plan(goals=["goal-A", "goal-B"])

    checkpoint = _build(
        plan=plan,
        executed_queries_by_task={},
        evidence_run=_outcome(),
    )

    assert checkpoint is None


def test_unresolved_after_search_is_a_verbatim_ordered_copy_of_review_missing() -> None:
    plan = _plan(goals=["goal-A"])
    missing = ["missing-b", "missing-a"]

    checkpoint = _build(
        plan=plan,
        executed_queries_by_task={0: ("q-a",)},
        evidence_run=_outcome(missing=missing),
    )

    assert checkpoint is not None
    assert checkpoint.unresolved_after_search == tuple(missing)


def test_as_of_is_carried_through_unchanged() -> None:
    plan = _plan(goals=["goal-A"])
    as_of = datetime(2026, 8, 4, 12, 30, tzinfo=UTC)

    checkpoint = _build(
        plan=plan,
        executed_queries_by_task={0: ("q-a",)},
        evidence_run=_outcome(),
        as_of=as_of,
    )

    assert checkpoint is not None
    assert checkpoint.as_of == as_of
