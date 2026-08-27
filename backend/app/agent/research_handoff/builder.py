"""既存工程の成果物からResearchHandoffの台帳と、整理の素材を組み立てる。

台帳はLLM呼び出しを追加しない。上流(plan正規化・EvidenceCollectionService)で
正規化済みの値をそのまま詰め替え、切り詰め・再正規化は行わない。上限違反は
`ResearchRunRecord`のPydantic validationが拒否する。

素材は整理工程へ見せるだけで、そのまま保存されることはない。
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
from app.agent.evidence_collection.contract import CollectedNews, CollectedTask
from app.agent.evidence_review import (
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.planning.contract import SearchPlan
from app.agent.research_handoff.contract import (
    HandoffMaterial,
    HandoffTaskMaterial,
)

__all__ = [
    "append_run_record",
    "build_handoff_material",
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


def build_handoff_material(
    *,
    question: str,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunResult,
    as_of: datetime,
) -> HandoffMaterial:
    """今回のRunで手に入ったものを、整理工程へ見せる形へ束ねる。

    精査が失敗したRunでも、何を叩いて何が集まったかは残っているため素材にする。
    """
    adopted_by_task = _adopted_by_task(evidence_run)
    return HandoffMaterial(
        question=question,
        as_of=as_of,
        tasks=tuple(
            HandoffTaskMaterial(
                research_goal=collected.research_goal,
                executed_queries=collected.executed_queries,
                external_collection=collected.report.external_collection,
                hit_headlines=_hit_headlines(collected),
                adopted=adopted_by_task.get(collected.task_index, ()),
            )
            for collected in collected_news.tasks
        ),
        review_missing=(
            evidence_run.review_missing
            if isinstance(evidence_run, EvidenceRunCompleted)
            else ()
        ),
    )


def _adopted_by_task(
    evidence_run: EvidenceRunResult,
) -> dict[int, tuple[tuple[str, str], ...]]:
    """採用evidenceを(claim, why_selected)の対としてtaskごとに束ねる。"""
    if not isinstance(evidence_run, EvidenceRunCompleted):
        return {}
    answer_evidence = evidence_run.answer_evidence
    adopted: dict[int, list[tuple[str, str]]] = {}
    for evidence in (
        *answer_evidence.internal_evidence,
        *answer_evidence.external_evidence,
    ):
        adopted.setdefault(evidence.task_index, []).append(
            (evidence.claim, evidence.why_selected)
        )
    return {task_index: tuple(items) for task_index, items in adopted.items()}


def _hit_headlines(collected: CollectedTask) -> tuple[str, ...]:
    """採用可否によらず、集まった記事の見出しを収集順に並べる。"""
    return tuple(
        [hit.content.title for hit in collected.internal_hits]
        + [hit.title for hit in collected.external_hits]
    )
