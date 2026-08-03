"""既存工程の成果物からResearchCheckpointを決定的に組み立てるbuilder。

LLM呼び出しを追加しない。上流(plan正規化・Researcher・Evidence Reviewer)で
正規化済みの値をそのまま詰め替え、切り詰め・再正規化は行わない。上限違反は
`ResearchCheckpoint`のPydantic validationが拒否する。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from app.agent.evidence_collection.evidence_review import EvidenceReviewOutcome
from app.agent.planning.contract import SearchPlan
from app.agent.research_checkpoint.contract import (
    ResearchCheckpoint,
    ResearchTaskRecord,
)

__all__ = ["build_research_checkpoint"]


def build_research_checkpoint(
    *,
    plan: SearchPlan,
    executed_queries_by_task: Mapping[int, tuple[str, ...]],
    review_outcome: EvidenceReviewOutcome,
    as_of: datetime,
) -> ResearchCheckpoint | None:
    """外部検索を実行できたtaskだけを記録する。記録可能taskが0件ならNone。"""

    adopted_claims_by_task: dict[int, list[str]] = {}
    for evidence in review_outcome.external_evidence:
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
        unresolved_after_search=tuple(review_outcome.missing),
    )
