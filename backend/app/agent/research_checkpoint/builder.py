"""既存工程の成果物からResearchCheckpointを決定的に組み立てるbuilder。

LLM呼び出しを追加しない。上流(plan正規化・Researcher・Evidence Reviewer)で
正規化済みの値をそのまま詰め替え、切り詰め・再正規化は行わない。上限違反は
`ResearchCheckpoint`のPydantic validationが拒否する。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import logfire

from app.agent.evidence_collection.contract import CollectedNews
from app.agent.evidence_review import EvidenceReviewOutcome
from app.agent.evidence_review.contract import RunReviewResult
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
    reviewed: RunReviewResult,
    as_of: datetime,
) -> ResearchCheckpoint | None:
    """精査失敗Runは記録せず、組み立て失敗は握って回答workflowを継続する。"""
    if reviewed.evidence.review.review == "failed":
        return None
    try:
        return build_research_checkpoint(
            plan=plan,
            executed_queries_by_task=collected_news.executed_queries_by_task,
            review_outcome=reviewed.review_outcome,
            as_of=as_of,
        )
    except Exception:
        logfire.warning("research_checkpoint_build_failed", failure_code="build_failed")
        return None


def build_research_checkpoint(
    *,
    plan: SearchPlan,
    executed_queries_by_task: Mapping[int, tuple[str, ...]],
    review_outcome: EvidenceReviewOutcome | None,
    as_of: datetime,
) -> ResearchCheckpoint | None:
    """外部検索を実行できたtaskだけを記録する。記録可能taskが0件ならNone。

    `review_outcome`がNoneなのは、全taskの候補が0件でevidence reviewを
    実行しなかったRunを表す。この場合adopted_claims・unresolved_after_search
    は空として組み立てる(採用可否・missingを判断するreviewが走っていないため)。
    """

    adopted_claims_by_task: dict[int, list[str]] = {}
    if review_outcome is not None:
        for evidence in review_outcome.answer_evidence.external_sources:
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

    unresolved_after_search = (
        tuple(review_outcome.missing) if review_outcome is not None else ()
    )
    return ResearchCheckpoint(
        as_of=as_of,
        tasks=tuple(tasks),
        unresolved_after_search=unresolved_after_search,
    )
