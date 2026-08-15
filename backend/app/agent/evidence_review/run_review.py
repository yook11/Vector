"""Run単位の精査。収集済み候補を1回の入力として精査し、根拠へ昇格する。"""

from __future__ import annotations

from datetime import datetime

from app.agent.evidence_collection.contract import CollectedNews
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
)
from app.agent.evidence_review.result import (
    AnswerEvidence,
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.evidence_review.reviewer import EvidenceReviewer

__all__ = ["review_collected_news"]


async def review_collected_news(
    *,
    collected_news: CollectedNews,
    reviewer: EvidenceReviewer,
    external: ExternalResearchRuntime,
    as_of: datetime,
) -> EvidenceRunResult:
    """候補ゼロは空の正常完了、精査失敗は専用の失敗結果で返す。"""
    if not collected_news.has_candidates:
        return EvidenceRunCompleted(
            answer_evidence=AnswerEvidence(),
            review_missing=(),
        )

    outcome = await reviewer.review(
        tasks=collected_news.tasks,
        as_of=as_of,
        reviewer_runtime=external.reviewer_runtime,
    )
    if outcome.failure_reason is not None:
        return EvidenceRunFailed(failure_reason=outcome.failure_reason)

    return EvidenceRunCompleted(
        answer_evidence=outcome.answer_evidence,
        review_missing=outcome.missing,
    )
