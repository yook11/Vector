"""Evidence Reviewer が共有する純粋なドメイン規則。

Run内の全taskの候補をtask_index昇順・通しindexでグループ化したprojectionの
構築と、選別結果からの出典再構築を持つ。
"""

from __future__ import annotations

from app.agent.evidence_collection.contract import CollectedTask
from app.agent.evidence_collection.external_search.contract import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)
from app.agent.evidence_review.contract import (
    EVIDENCE_REVIEW_ADOPTION_LIMIT,
    EvidenceCandidateProjection,
    EvidenceReviewDraft,
    EvidenceReviewResult,
    EvidenceReviewTaskGroup,
    InternalArticleEvidence,
    ReviewSelection,
)

__all__ = [
    "EVIDENCE_REVIEW_TIMEOUT_SECONDS",
    "REVIEWER_ERROR_REASON",
    "REVIEWER_TIMEOUT_REASON",
    "build_review_evidence",
    "build_review_task_groups",
    "finalize_review_draft",
    "resolve_reviewer_failure_reason",
]

EVIDENCE_REVIEW_TIMEOUT_SECONDS = 30
REVIEWER_TIMEOUT_REASON = "reviewer_timeout"
REVIEWER_ERROR_REASON = "reviewer_error"


def build_review_task_groups(
    tasks: list[CollectedTask],
) -> tuple[EvidenceReviewTaskGroup, ...]:
    """Run内の全taskをtask_index昇順に並べ、通しindexのcandidate群でグループ化する。

    候補が内外ともゼロのtaskも欠番にせず空groupとして残す。
    """
    ordered_tasks = sorted(tasks, key=lambda task: task.task_index)
    groups: list[EvidenceReviewTaskGroup] = []
    next_index = 0
    for task in ordered_tasks:
        candidates, next_index = _build_group_candidates(task, start_index=next_index)
        groups.append(
            EvidenceReviewTaskGroup(
                task_index=task.task_index,
                research_goal=task.research_goal,
                candidates=candidates,
            )
        )
    return tuple(groups)


def _build_group_candidates(
    task: CollectedTask,
    *,
    start_index: int,
) -> tuple[tuple[EvidenceCandidateProjection, ...], int]:
    """内部候補を先・外部候補を後にした、通しindexのgroup内candidate列を作る。"""
    projection = [
        EvidenceCandidateProjection(
            index=start_index + index,
            title=hit.content.title,
            source_name=None,
            published_at=hit.content.published_at,
            snippet=_internal_candidate_snippet(hit),
        )
        for index, hit in enumerate(task.internal_hits)
    ]
    offset = start_index + len(task.internal_hits)
    projection.extend(
        EvidenceCandidateProjection(
            index=offset + position,
            title=candidate.title,
            source_name=candidate.source_name,
            published_at=candidate.published_at,
            snippet=candidate.snippet,
        )
        for position, candidate in enumerate(task.external_candidates)
    )
    next_index = offset + len(task.external_candidates)
    return tuple(projection), next_index


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
    tasks: list[CollectedTask],
    selection_result: EvidenceReviewResult,
) -> tuple[list[InternalArticleEvidence], list[ExternalSearchEvidence], int]:
    """Run全体の通しindexのselectionから、範囲外/重複/Run単位上限超過を

    dropしつつ出典と所属taskを復元する。
    """
    index_map = _build_index_map(tasks)
    internal_evidence: list[InternalArticleEvidence] = []
    external_evidence: list[ExternalSearchEvidence] = []
    selected_indexes: set[int] = set()
    dropped_selection_count = 0
    total_count = len(index_map)

    for selection in selection_result.selections:
        index = selection.candidate_index
        if (
            index >= total_count
            or index in selected_indexes
            or len(internal_evidence) + len(external_evidence)
            >= EVIDENCE_REVIEW_ADOPTION_LIMIT
        ):
            dropped_selection_count += 1
            continue

        selected_indexes.add(index)
        task_index, hit, candidate = index_map[index]
        source_ref = f"{task_index}-{index}"
        if hit is not None:
            internal_evidence.append(
                _build_internal_evidence(
                    hit=hit,
                    selection=selection,
                    source_ref=source_ref,
                    task_index=task_index,
                )
            )
        else:
            assert candidate is not None  # noqa: S101
            external_evidence.append(
                _build_external_evidence(
                    candidate=candidate,
                    selection=selection,
                    source_ref=source_ref,
                    task_index=task_index,
                )
            )

    return internal_evidence, external_evidence, dropped_selection_count


type _IndexMapEntry = tuple[
    int, InternalArticleSearchHit | None, ExternalSearchCandidate | None
]


def _build_index_map(tasks: list[CollectedTask]) -> list[_IndexMapEntry]:
    """build_review_task_groupsと同じ並びで、通しindex→(task_index, 候補)を作る。"""
    index_map: list[_IndexMapEntry] = []
    for task in sorted(tasks, key=lambda task: task.task_index):
        for hit in task.internal_hits:
            index_map.append((task.task_index, hit, None))
        for candidate in task.external_candidates:
            index_map.append((task.task_index, None, candidate))
    return index_map


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
