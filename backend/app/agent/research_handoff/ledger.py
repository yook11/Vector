"""何を狙って何を叩いたかの台帳を、既存工程の成果物から決定的に組み立てる。

LLM呼び出しを追加しない。上流(plan正規化・EvidenceCollectionService)で正規化
済みの値をそのまま詰め替え、切り詰め・再正規化は行わない。上限違反は
`ResearchRunRecord`のPydantic validationが拒否する。

何が得られたかは台帳に持たず、整理側(ResearchHandoffの3本)が持つ。
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
    """今回の台帳を末尾へ積む。整理は前回の値をそのまま引き継ぐ。"""
    if previous is None:
        return ResearchHandoff(updated_at=record.as_of, runs=(record,))
    return ResearchHandoff(
        updated_at=record.as_of,
        runs=previous.runs + (record,),
        collected_overview=previous.collected_overview,
        unresolved_points=previous.unresolved_points,
        next_search_guidance=previous.next_search_guidance,
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
            as_of=as_of,
        )
    except Exception:
        logfire.warning("research_handoff_build_failed", failure_code="build_failed")
        return None


def build_research_run_record(
    *,
    plan: SearchPlan,
    executed_queries_by_task: Mapping[int, tuple[str, ...]],
    as_of: datetime,
) -> ResearchRunRecord | None:
    """外部検索を実行できたtaskだけを記録する。記録可能taskが0件ならNone。"""
    tasks = tuple(
        ResearchTaskRecord(
            research_goal=task.research_goal,
            executed_queries=executed_queries_by_task[task_index],
        )
        for task_index, task in enumerate(plan.research_tasks)
        if executed_queries_by_task.get(task_index)
    )
    if not tasks:
        return None
    return ResearchRunRecord(as_of=as_of, tasks=tasks)
