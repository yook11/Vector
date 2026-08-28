"""今回のRunから、整理工程の入力を組み立てる。

収集・精査の成果物は記事本文やURLまで持つが、整理に要るのは「何を狙い、何を
叩き、何が集まり、何を採ったか」だけである。台帳の追記は
`ResearchHandoff.with_run` に任せ、投影先をこの型に限ることで整理工程へ渡る
範囲を型で閉じる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import logfire

from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    TaskExternalCollectionStatus,
)
from app.agent.evidence_review import (
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.research_handoff.handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)

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
        previous: ResearchHandoff | None,
        question: str,
        collected_news: CollectedNews,
        evidence_run: EvidenceRunResult,
        as_of: datetime,
    ) -> ResearchHandoffInput | None:
        """前回の申し送りと今回の成果物から入力を組み立てる。作れなければNone。"""
        if isinstance(evidence_run, EvidenceRunFailed):
            return None
        try:
            record = _build_research_run_record(
                collected_news=collected_news,
                as_of=as_of,
            )
        except Exception:
            logfire.warning(
                "research_handoff_build_failed",
                failure_code="build_failed",
            )
            return None
        if record is None:
            return None
        adopted_by_task = _adopted_by_task(evidence_run)
        return cls(
            handoff=ResearchHandoff.with_run(previous=previous, record=record),
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
                for collected in _tasks_in_index_order(collected_news)
            ),
            review_missing=evidence_run.review_missing,
        )


def _build_research_run_record(
    *,
    collected_news: CollectedNews,
    as_of: datetime,
) -> ResearchRunRecord | None:
    """外部検索を実行できたtaskだけを記録する。記録可能taskが0件ならNone。"""
    tasks = tuple(
        ResearchTaskRecord(
            research_goal=collected.research_goal,
            executed_queries=collected.executed_queries,
        )
        for collected in _tasks_in_index_order(collected_news)
        if collected.executed_queries
    )
    if not tasks:
        return None
    return ResearchRunRecord(as_of=as_of, tasks=tasks)


def _tasks_in_index_order(collected_news: CollectedNews) -> tuple[CollectedTask, ...]:
    return tuple(sorted(collected_news.tasks, key=lambda task: task.task_index))


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
