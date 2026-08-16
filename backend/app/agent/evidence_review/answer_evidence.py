"""回答用根拠の復元と、精査Runの確定。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.contract import (
    ANSWER_EVIDENCE_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
    MISSING_ITEM_MAX_CHARS,
)
from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EVIDENCE_WHY_SELECTED_MAX_CHARS,
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)
from app.agent.evidence_review.preparation import EvidenceReviewPreparation
from app.agent.evidence_review.selection import EvidenceReviewerResponse
from app.shared.security.safe_url import SafeUrl

if TYPE_CHECKING:
    from app.agent.evidence_review.selection import EvidenceReviewerSelection

__all__ = [
    "AnswerEvidence",
    "EvidenceRunCompleted",
    "EvidenceRunFailed",
    "EvidenceRunResult",
    "ExternalSearchEvidence",
    "InternalArticleEvidence",
]


class InternalArticleEvidence(BaseModel):
    """内部記事に対するreviewerの精査済み採用1件。claimを持つ。"""

    model_config = ConfigDict(frozen=True)

    source_ref: str = Field(min_length=1)
    task_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1,
        max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS,
    )
    assessment_id: int = Field(gt=0)
    curation_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list)
    published_at: datetime | None = None

    @classmethod
    def from_reviewed_hit(
        cls,
        hit: InternalArticleSearchHit,
        *,
        selection: EvidenceReviewerSelection,
        source_ref: str,
        task_index: int,
    ) -> Self:
        return cls(
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


class ExternalSearchEvidence(BaseModel):
    """外部URLに対するreviewerの精査済み採用1件。claimを持つ。"""

    model_config = ConfigDict(frozen=True)

    source_ref: str = Field(min_length=1)
    task_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1,
        max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS,
    )
    url: SafeUrl
    title: str = Field(min_length=1)
    snippet: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None

    @classmethod
    def from_reviewed_candidate(
        cls,
        candidate: ExternalSearchCandidate,
        *,
        selection: EvidenceReviewerSelection,
        source_ref: str,
        task_index: int,
    ) -> Self:
        return cls(
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


class AnswerEvidence(BaseModel):
    """出典を復元し、重複排除と件数制限を終えた回答用Evidence。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    internal_articles: tuple[InternalArticleEvidence, ...] = ()
    external_sources: tuple[ExternalSearchEvidence, ...] = ()

    @classmethod
    def from_reviewer_response(
        cls,
        *,
        preparation: EvidenceReviewPreparation,
        reviewer_response: EvidenceReviewerResponse,
    ) -> AnswerEvidence:
        """Reviewerの選択から出典を復元し、一意な回答用Evidenceを構築する。"""
        internal_articles: list[InternalArticleEvidence] = []
        external_sources: list[ExternalSearchEvidence] = []
        selected_indexes: set[int] = set()
        seen_internal_source_identities: set[tuple[int, int]] = set()
        seen_external_source_identities: set[tuple[int, str]] = set()

        for selection in reviewer_response.selections:
            index = selection.option_index
            origin = preparation.resolve_option_origin(index)
            if origin is None or index in selected_indexes:
                continue

            selected_indexes.add(index)
            source_ref = f"{origin.task_index}-{index}"
            if isinstance(origin.search_result, InternalArticleSearchHit):
                curation_id = origin.search_result.article.curation_id
                source_identity = (origin.task_index, curation_id)
                if source_identity in seen_internal_source_identities:
                    continue
                seen_internal_source_identities.add(source_identity)
                internal_articles.append(
                    InternalArticleEvidence.from_reviewed_hit(
                        origin.search_result,
                        selection=selection,
                        source_ref=source_ref,
                        task_index=origin.task_index,
                    )
                )
            else:
                url = str(origin.search_result.url)
                source_identity = (origin.task_index, url)
                if source_identity in seen_external_source_identities:
                    continue
                seen_external_source_identities.add(source_identity)
                external_sources.append(
                    ExternalSearchEvidence.from_reviewed_candidate(
                        origin.search_result,
                        selection=selection,
                        source_ref=source_ref,
                        task_index=origin.task_index,
                    )
                )

            if len(internal_articles) + len(external_sources) >= ANSWER_EVIDENCE_LIMIT:
                break

        return cls(
            internal_articles=tuple(internal_articles),
            external_sources=tuple(external_sources),
        )

    @property
    def count(self) -> int:
        return len(self.internal_articles) + len(self.external_sources)

    @property
    def task_indexes(self) -> set[int]:
        return {
            item.task_index
            for item in (*self.internal_articles, *self.external_sources)
        }

    @model_validator(mode="after")
    def _validate_answer_evidence(self) -> Self:
        if self.count > ANSWER_EVIDENCE_LIMIT:
            raise ValueError("answer evidence exceeds input limit")

        internal_source_identities = [
            (item.task_index, item.curation_id) for item in self.internal_articles
        ]
        if len(internal_source_identities) != len(set(internal_source_identities)):
            raise ValueError(
                "internal answer evidence article must be unique within a task"
            )

        external_source_identities = [
            (item.task_index, str(item.url)) for item in self.external_sources
        ]
        if len(external_source_identities) != len(set(external_source_identities)):
            raise ValueError(
                "external answer evidence URL must be unique within a task"
            )

        source_refs = [item.source_ref for item in self.internal_articles] + [
            item.source_ref for item in self.external_sources
        ]
        if len(source_refs) != len(set(source_refs)):
            raise ValueError("answer evidence source_ref must be unique")
        return self


class EvidenceRunCompleted(BaseModel):
    """Evidence Runを正常に完了し、回答用結果を確定できた。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_evidence: AnswerEvidence
    review_missing: tuple[
        Annotated[str, StringConstraints(max_length=MISSING_ITEM_MAX_CHARS)],
        ...,
    ] = Field(max_length=EVIDENCE_REVIEW_MISSING_LIMIT)


class EvidenceRunFailed(BaseModel):
    """Evidence Runを正常に完了できなかった。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_reason: str = Field(min_length=1)


EvidenceRunResult = EvidenceRunCompleted | EvidenceRunFailed
