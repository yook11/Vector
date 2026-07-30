"""Evidence Reviewer が共有する純粋なドメイン規則。

内部・外部統合candidate列を対象とする候補projectionの構築と、
選別結果からの出典再構築を持つ。
"""

from __future__ import annotations

from app.agent.evidence_collection.evidence_review.contract import (
    EVIDENCE_REVIEW_ADOPTION_LIMIT_PER_TASK,
    EvidenceCandidateInput,
    EvidenceReviewDraft,
    EvidenceReviewResult,
    InternalArticleEvidence,
    ReviewSelection,
)
from app.agent.evidence_collection.external_search.contract import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)

__all__ = [
    "EVIDENCE_REVIEW_TIMEOUT_SECONDS",
    "REVIEWER_ERROR_REASON",
    "REVIEWER_TIMEOUT_REASON",
    "build_review_candidate_projection",
    "build_review_evidence",
    "finalize_review_draft",
    "resolve_reviewer_failure_reason",
]

EVIDENCE_REVIEW_TIMEOUT_SECONDS = 30
REVIEWER_TIMEOUT_REASON = "reviewer_timeout"
REVIEWER_ERROR_REASON = "reviewer_error"


def build_review_candidate_projection(
    *,
    internal_hits: list[InternalArticleSearchHit],
    external_candidates: list[ExternalSearchCandidate],
) -> tuple[EvidenceCandidateInput, ...]:
    """内部候補を先・外部候補を後にした単一index空間のprojectionを作る。"""
    projection = [
        EvidenceCandidateInput(
            index=index,
            title=hit.content.title,
            source_name=None,
            published_at=hit.content.published_at,
            snippet=_internal_candidate_snippet(hit),
        )
        for index, hit in enumerate(internal_hits)
    ]
    offset = len(internal_hits)
    projection.extend(
        EvidenceCandidateInput(
            index=offset + position,
            title=candidate.title,
            source_name=candidate.source_name,
            published_at=candidate.published_at,
            snippet=candidate.snippet,
        )
        for position, candidate in enumerate(external_candidates)
    )
    return tuple(projection)


def _internal_candidate_snippet(hit: InternalArticleSearchHit) -> str:
    content = hit.content
    if not content.key_points:
        combined = content.summary
    else:
        key_points = "\n".join(f"- {point}" for point in content.key_points)
        combined = f"{content.summary}\n{key_points}"
    return combined[:CANDIDATE_SNIPPET_MAX_CHARS]


def build_review_evidence(
    *,
    task_index: int,
    internal_hits: list[InternalArticleSearchHit],
    external_candidates: list[ExternalSearchCandidate],
    selection_result: EvidenceReviewResult,
) -> tuple[list[InternalArticleEvidence], list[ExternalSearchEvidence], int]:
    """統合index空間のselectionから、範囲外/重複/上限超過をdropしつつ出典を復元する。"""
    internal_evidence: list[InternalArticleEvidence] = []
    external_evidence: list[ExternalSearchEvidence] = []
    selected_indexes: set[int] = set()
    dropped_selection_count = 0
    internal_count = len(internal_hits)
    total_count = internal_count + len(external_candidates)

    for selection in selection_result.selections:
        index = selection.candidate_index
        if (
            index >= total_count
            or index in selected_indexes
            or len(internal_evidence) + len(external_evidence)
            >= EVIDENCE_REVIEW_ADOPTION_LIMIT_PER_TASK
        ):
            dropped_selection_count += 1
            continue

        selected_indexes.add(index)
        source_ref = f"{task_index}-{index}"
        if index < internal_count:
            internal_evidence.append(
                _build_internal_evidence(
                    hit=internal_hits[index],
                    selection=selection,
                    source_ref=source_ref,
                    task_index=task_index,
                )
            )
        else:
            external_evidence.append(
                _build_external_evidence(
                    candidate=external_candidates[index - internal_count],
                    selection=selection,
                    source_ref=source_ref,
                    task_index=task_index,
                )
            )

    return internal_evidence, external_evidence, dropped_selection_count


def _build_internal_evidence(
    *,
    hit: InternalArticleSearchHit,
    selection: ReviewSelection,
    source_ref: str,
    task_index: int,
) -> InternalArticleEvidence:
    return InternalArticleEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim=selection.claim,
        why_selected=selection.why_selected,
        assessment_id=hit.assessment_id,
        curation_id=hit.article.curation_id,
        title=hit.content.title,
        summary=hit.content.summary,
        key_points=hit.content.key_points,
        published_at=hit.content.published_at,
    )


def _build_external_evidence(
    *,
    candidate: ExternalSearchCandidate,
    selection: ReviewSelection,
    source_ref: str,
    task_index: int,
) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim=selection.claim,
        why_selected=selection.why_selected,
        url=candidate.url,
        title=candidate.title,
        snippet=candidate.snippet,
        published_at=candidate.published_at,
        source_name=candidate.source_name,
    )


def finalize_review_draft(draft: EvidenceReviewDraft) -> EvidenceReviewResult:
    return EvidenceReviewResult.from_raw(
        selections=[selection.model_dump() for selection in draft.selections],
        missing=draft.missing,
    )


def resolve_reviewer_failure_reason(
    *,
    reason: str | None,
    code: str | None,
) -> str:
    if reason is not None:
        return reason
    if code is not None:
        return code
    return REVIEWER_ERROR_REASON
