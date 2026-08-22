"""既存工程の成果物からResearchCheckpointを決定的に組み立てるbuilder。

LLM呼び出しを追加しない。上流(plan正規化・EvidenceCollectionService・
Evidence Reviewer)で
正規化済みの値をそのまま詰め替え、切り詰め・再正規化は行わない。上限違反は
`ResearchCheckpoint`のPydantic validationが拒否する。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import logfire

from app.agent.evidence_collection.contract import CollectedNews
from app.agent.evidence_review import (
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.planning.contract import SearchPlan
from app.agent.research_checkpoint.contract import (
    ResearchCheckpoint,
    ResearchTaskRecord,
)

__all__ = ["build_research_checkpoint", "build_research_checkpoint_or_none"]


def build_research_checkpoint_or_none(
    *,
    plan: SearchPlan,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunResult,
    as_of: datetime,
) -> ResearchCheckpoint | None:
    """精査失敗Runは記録せず、組み立て失敗は握って回答workflowを継続する。"""
    if isinstance(evidence_run, EvidenceRunFailed):
        return None
    try:
        return build_research_checkpoint(
            plan=plan,
            executed_queries_by_task=collected_news.executed_queries_by_task,
            evidence_run=evidence_run,
            as_of=as_of,
        )
    except Exception:
        logfire.warning("research_checkpoint_build_failed", failure_code="build_failed")
        return None


def build_research_checkpoint(
    *,
    plan: SearchPlan,
    executed_queries_by_task: Mapping[int, tuple[str, ...]],
    evidence_run: EvidenceRunCompleted,
    as_of: datetime,
) -> ResearchCheckpoint | None:
    """外部検索を実行できたtaskだけを記録する。記録可能taskが0件ならNone。

    検索ヒットゼロでreviewerを実行しなかったRunも、Evidenceと
    review_missingが空の正常完了として決定的に組み立てる。
    """
    plan_task_indexes = set(range(len(plan.research_tasks)))
    if not evidence_run.answer_evidence.task_indexes <= plan_task_indexes:
        raise ValueError("answer evidence must reference a planned task")

    adopted_claims_by_task: dict[int, list[str]] = {}
    for evidence in evidence_run.answer_evidence.external_evidence:
        adopted_claims_by_task.setdefault(evidence.task_index, []).append(
            evidence.claim
        )

    tasks: list[ResearchTaskRecord] = []
    for task_index, task in enumerate(plan.research_tasks):
        executed_queries = executed_queries_by_task.get(task_index, ())
        if not executed_queries:
            continue
        tasks.append(
            ResearchTaskRecord(
                research_goal=task.research_goal,
                executed_queries=executed_queries,
                adopted_claims=tuple(adopted_claims_by_task.get(task_index, [])),
            )
        )

    if not tasks:
        return None

    return ResearchCheckpoint(
        as_of=as_of,
        tasks=tuple(tasks),
        unresolved_after_search=evidence_run.review_missing,
    )
