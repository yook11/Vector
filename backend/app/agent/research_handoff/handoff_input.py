"""申し送りを書き直す前に要る情報を、上流工程の成果物から投影する。

収集・精査の成果物は記事本文やURLまで持つが、整理に要るのは「何を狙い、何を
叩き、何が集まり、何を採ったか」だけである。投影先をこの型に限ることで、
整理工程へ渡る範囲を型で閉じる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.agent.contract import ResearchHandoff
from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    TaskExternalCollectionStatus,
)
from app.agent.evidence_review import EvidenceRunCompleted, EvidenceRunResult

__all__ = ["ResearchHandoffInput", "SearchedTask"]


@dataclass(frozen=True, slots=True)
class SearchedTask:
    """1 taskで何を狙い、何を叩き、何が集まり、何を採ったか。"""

    research_goal: str
    executed_queries: tuple[str, ...]
    # 外部収集の結末。「情報が無かった」と「検索できなかった」の区別に要る。
    external_collection: TaskExternalCollectionStatus
    # 採用されなかったものも含むヒット記事の見出し。本文は渡さない。
    hit_headlines: tuple[str, ...]
    # 採用したevidenceのclaimと、reviewerがそれを選んだ理由。
    adopted: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ResearchHandoffInput:
    """整理の1回に要る情報。書き直す対象と、今回の調査で分かったこと。"""

    handoff: ResearchHandoff
    question: str
    as_of: datetime
    tasks: tuple[SearchedTask, ...]
    review_missing: tuple[str, ...]

    @classmethod
    def from_run(
        cls,
        *,
        handoff: ResearchHandoff,
        question: str,
        collected_news: CollectedNews,
        evidence_run: EvidenceRunResult,
        as_of: datetime,
    ) -> ResearchHandoffInput:
        """精査が失敗したRunでも、何を叩いて何が集まったかは残るため投影する。"""
        adopted_by_task = _adopted_by_task(evidence_run)
        return cls(
            handoff=handoff,
            question=question,
            as_of=as_of,
            tasks=tuple(
                SearchedTask(
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
