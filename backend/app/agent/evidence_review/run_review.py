"""Run単位の精査。収集済み候補を1回の入力として精査し、根拠へ昇格する。"""

from __future__ import annotations

from datetime import datetime

from app.agent.evidence_collection.contract import CollectedNews, ResearchTaskReport
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
)
from app.agent.evidence_review.contract import (
    AnswerEvidence,
    EvidenceReviewReport,
    ReviewedEvidence,
    RunReviewResult,
)
from app.agent.evidence_review.reviewer import EvidenceReviewer

__all__ = ["review_collected_news"]


async def review_collected_news(
    *,
    collected_news: CollectedNews,
    reviewer: EvidenceReviewer,
    external: ExternalResearchRuntime,
    as_of: datetime,
) -> RunReviewResult:
    """候補ゼロのRunは精査せず、精査失敗のRunは根拠ゼロで閉じる。"""
    task_reports = [collected.report for collected in collected_news.tasks]
    if not collected_news.has_candidates:
        return RunReviewResult(
            evidence=_closed_evidence(
                review=EvidenceReviewReport(review="skipped_empty"),
                task_reports=task_reports,
                collected_news=collected_news,
            ),
            review_outcome=None,
        )

    outcome = await reviewer.review(
        tasks=collected_news.tasks,
        as_of=as_of,
        reviewer_runtime=external.reviewer_runtime,
    )
    if outcome.failure_reason is not None:
        return RunReviewResult(
            evidence=_closed_evidence(
                review=EvidenceReviewReport(
                    review="failed",
                    review_failure_reason=outcome.failure_reason,
                ),
                task_reports=task_reports,
                collected_news=collected_news,
            ),
            review_outcome=None,
        )

    return RunReviewResult(
        evidence=ReviewedEvidence(
            answer_evidence=outcome.answer_evidence,
            requested_external_agent_count=collected_news.requested_agent_count,
            effective_external_agent_count=collected_news.effective_agent_count,
            task_reports=task_reports,
            review=EvidenceReviewReport(
                review="succeeded",
                internal_evidence_count=len(outcome.answer_evidence.internal_articles),
                external_evidence_count=len(outcome.answer_evidence.external_sources),
                missing=outcome.missing,
            ),
        ),
        review_outcome=outcome,
    )


def _closed_evidence(
    *,
    review: EvidenceReviewReport,
    task_reports: list[ResearchTaskReport],
    collected_news: CollectedNews,
) -> ReviewedEvidence:
    """精査を呼ばなかった/失敗したRunを根拠ゼロで閉じる。"""
    return ReviewedEvidence(
        answer_evidence=AnswerEvidence(),
        requested_external_agent_count=collected_news.requested_agent_count,
        effective_external_agent_count=collected_news.effective_agent_count,
        task_reports=task_reports,
        review=review,
    )
