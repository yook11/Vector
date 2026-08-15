"""Evidence Reviewer が共有する純粋なドメイン規則。

EvidenceReviewPreparationがreviewerに見せる投影と番号の控えを同一の採番で構築し、
build_review_evidenceが選別結果を採用規則(実在/重複/上限)に通して出典を復元する。
"""

from __future__ import annotations

from dataclasses import dataclass

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
    "EvidenceReviewPreparation",
    "ReviewCandidateEntry",
    "build_review_evidence",
    "finalize_review_draft",
    "resolve_reviewer_failure_reason",
]

EVIDENCE_REVIEW_TIMEOUT_SECONDS = 30
REVIEWER_TIMEOUT_REASON = "reviewer_timeout"
REVIEWER_ERROR_REASON = "reviewer_error"


@dataclass(frozen=True, slots=True)
class ReviewCandidateEntry:
    """通し番号1つに対応する候補の控え。EvidenceReviewPreparation.from_tasksだけが生成する。"""

    index: int
    task_index: int
    source: InternalArticleSearchHit | ExternalSearchCandidate


@dataclass(frozen=True, slots=True)
class EvidenceReviewPreparation:
    """reviewerに見せる投影と、番号から元候補を引く控えを、同一の採番で保持する。"""

    task_groups: tuple[EvidenceReviewTaskGroup, ...]
    _candidate_entries: tuple[ReviewCandidateEntry, ...]

    @classmethod
    def from_tasks(cls, tasks: list[CollectedTask]) -> EvidenceReviewPreparation:
        """task_index昇順・グループ内は内部先・外部後の1ループで、投影と控えを同時に採番する。"""
        ordered_tasks = sorted(tasks, key=lambda task: task.task_index)
        review_tasks: list[EvidenceReviewTaskGroup] = []
        entries: list[ReviewCandidateEntry] = []
        for task in ordered_tasks:
            candidates: list[EvidenceCandidateProjection] = []
            for hit in task.internal_hits:
                index = len(entries)
                candidates.append(
                    EvidenceCandidateProjection(
                        index=index,
                        title=hit.content.title,
                        source_name=None,
                        published_at=hit.content.published_at,
                        snippet=_internal_candidate_snippet(hit),
                    )
                )
                entries.append(
                    ReviewCandidateEntry(
                        index=index, task_index=task.task_index, source=hit
                    )
                )
            for candidate in task.external_candidates:
                index = len(entries)
                candidates.append(
                    EvidenceCandidateProjection(
                        index=index,
                        title=candidate.title,
                        source_name=candidate.source_name,
                        published_at=candidate.published_at,
                        snippet=candidate.snippet,
                    )
                )
                entries.append(
                    ReviewCandidateEntry(
                        index=index, task_index=task.task_index, source=candidate
                    )
                )
            review_tasks.append(
                EvidenceReviewTaskGroup(
                    task_index=task.task_index,
                    research_goal=task.research_goal,
                    candidates=tuple(candidates),
                )
            )
        return cls(task_groups=tuple(review_tasks), _candidate_entries=tuple(entries))

    def resolve_candidate(self, candidate_index: int) -> ReviewCandidateEntry | None:
        """見せた番号だけを控えへ解決する。範囲外(負数含む)はNone。"""
        if 0 <= candidate_index < len(self._candidate_entries):
            return self._candidate_entries[candidate_index]
        return None


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
    preparation: EvidenceReviewPreparation,
    selection_result: EvidenceReviewResult,
) -> tuple[list[InternalArticleEvidence], list[ExternalSearchEvidence], int]:
    """selectionを採用規則(実在/重複/Run単位上限)に通し、出典と所属taskを復元する。

    採用されなかったselectionは理由を問わずdropped件数に数える(選択は黙って消えない)。
    """
    internal_evidence: list[InternalArticleEvidence] = []
    external_evidence: list[ExternalSearchEvidence] = []
    selected_indexes: set[int] = set()
    dropped_selection_count = 0

    for selection in selection_result.selections:
        index = selection.candidate_index
        entry = preparation.resolve_candidate(index)
        if (
            entry is None
            or index in selected_indexes
            or len(internal_evidence) + len(external_evidence)
            >= EVIDENCE_REVIEW_ADOPTION_LIMIT
        ):
            dropped_selection_count += 1
            continue

        selected_indexes.add(index)
        source_ref = f"{entry.task_index}-{index}"
        if isinstance(entry.source, InternalArticleSearchHit):
            internal_evidence.append(
                _build_internal_evidence(
                    hit=entry.source,
                    selection=selection,
                    source_ref=source_ref,
                    task_index=entry.task_index,
                )
            )
        else:
            external_evidence.append(
                _build_external_evidence(
                    candidate=entry.source,
                    selection=selection,
                    source_ref=source_ref,
                    task_index=entry.task_index,
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
