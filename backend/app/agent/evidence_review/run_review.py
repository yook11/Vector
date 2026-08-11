"""Run単位の精査。収集済み候補を1回の入力として精査し、根拠へ昇格する。"""

from __future__ import annotations

from datetime import datetime

from app.agent.evidence_collection.contract import CollectedNews, ResearchTaskReport
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
    ExternalSearchOutcome,
)
from app.agent.evidence_review.contract import (
    EvidenceReviewReport,
    InternalArticleEvidence,
    ReviewedEvidence,
    ReviewTaskCandidates,
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
        tasks=[
            ReviewTaskCandidates(
                task_index=collected.task_index,
                research_goal=collected.research_goal,
                internal_hits=collected.internal_hits,
                external_candidates=collected.external_candidates,
            )
            for collected in collected_news.tasks
        ],
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

    deduplicated_internal_evidence, internal_deduplicated_count = (
        _deduplicate_internal_evidence_by_curation_id(outcome.internal_evidence)
    )
    return RunReviewResult(
        evidence=ReviewedEvidence(
            internal_evidence=deduplicated_internal_evidence,
            internal_deduplicated_count=internal_deduplicated_count,
            external_search=ExternalSearchOutcome(
                evidence=outcome.external_evidence,
                requested_agent_count=collected_news.requested_agent_count,
                effective_agent_count=collected_news.effective_agent_count,
            ),
            task_reports=task_reports,
            review=EvidenceReviewReport(
                review="succeeded",
                internal_evidence_count=len(outcome.internal_evidence),
                external_evidence_count=len(outcome.external_evidence),
                dropped_selection_count=outcome.dropped_selection_count,
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
        internal_evidence=[],
        internal_deduplicated_count=0,
        external_search=ExternalSearchOutcome(
            evidence=[],
            requested_agent_count=collected_news.requested_agent_count,
            effective_agent_count=collected_news.effective_agent_count,
        ),
        task_reports=task_reports,
        review=review,
    )


def _deduplicate_internal_evidence_by_curation_id(
    evidence: list[InternalArticleEvidence],
) -> tuple[list[InternalArticleEvidence], int]:
    """内部記事識別子(curation_id)の先勝ちでrun単位の重複を除く。"""
    deduplicated: list[InternalArticleEvidence] = []
    seen_curation_ids: set[int] = set()
    dropped_count = 0
    for item in evidence:
        if item.curation_id in seen_curation_ids:
            dropped_count += 1
            continue
        deduplicated.append(item)
        seen_curation_ids.add(item.curation_id)
    return deduplicated, dropped_count
