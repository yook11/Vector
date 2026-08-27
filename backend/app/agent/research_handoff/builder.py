"""既存工程の成果物からResearchHandoffを決定的に組み立てるbuilder。

LLM呼び出しを追加しない。上流(plan正規化・EvidenceCollectionService・
Evidence Reviewer)で正規化済みの値をそのまま詰め替え、切り詰め・再正規化は
行わない。上限違反は`ResearchRunRecord`のPydantic validationが拒否する。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import logfire

from app.agent.contract import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)
from app.agent.evidence_collection.contract import CollectedNews
from app.agent.evidence_review import (
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.planning.contract import SearchPlan

__all__ = [
    "append_run_record",
    "build_research_run_record",
    "build_research_run_record_or_none",
]


def append_run_record(
    *,
    previous: ResearchHandoff | None,
    record: ResearchRunRecord,
) -> ResearchHandoff:
    """今回の記録を末尾へ積む。判断層は前回の値をそのまま引き継ぐ。"""
    if previous is None:
        return ResearchHandoff(updated_at=record.as_of, runs=(record,))
    return ResearchHandoff(
        updated_at=record.as_of,
        standing_inquiry=previous.standing_inquiry,
        runs=previous.runs + (record,),
        next_directives=previous.next_directives,
    )


def build_research_run_record_or_none(
    *,
    plan: SearchPlan,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunResult,
    as_of: datetime,
) -> ResearchRunRecord | None:
    """精査失敗Runは記録せず、組み立て失敗は握って回答workflowを継続する。"""
    if isinstance(evidence_run, EvidenceRunFailed):
        return None
    try:
        return build_research_run_record(
            plan=plan,
            executed_queries_by_task=collected_news.executed_queries_by_task,
            evidence_run=evidence_run,
            as_of=as_of,
        )
    except Exception:
        logfire.warning("research_handoff_build_failed", failure_code="build_failed")
        return None


def build_research_run_record(
    *,
    plan: SearchPlan,
    executed_queries_by_task: Mapping[int, tuple[str, ...]],
    evidence_run: EvidenceRunCompleted,
    as_of: datetime,
) -> ResearchRunRecord | None:
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

    return ResearchRunRecord(
        as_of=as_of,
        tasks=tuple(tasks),
        unresolved_after_search=evidence_run.review_missing,
    )
