"""回答用根拠の復元と、精査Runの確定。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.contract import (
    EVIDENCE_REVIEW_MISSING_LIMIT,
    MISSING_ITEM_MAX_CHARS,
)
from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EVIDENCE_WHY_SELECTED_MAX_CHARS,
    ExternalSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)
from app.agent.evidence_review.preparation import EvidenceReviewPreparation
from app.shared.security.safe_url import SafeUrl

if TYPE_CHECKING:
    # 件数上限の正本をこのモジュールが持つため、selection側が実行時にこちらを
    # importする。逆向きは型注釈だけなので実行時依存を持たない。
    from app.agent.evidence_review.selection import (
        EvidenceReviewerResponse,
        EvidenceReviewerSelection,
    )

__all__ = [
    "ANSWER_EVIDENCE_LIMIT",
    "AnswerEvidence",
    "EvidenceRunCompleted",
    "EvidenceRunFailed",
    "EvidenceRunResult",
    "ExternalSearchEvidence",
    "InternalArticleEvidence",
]

ANSWER_EVIDENCE_LIMIT: Final[int] = 15


class InternalArticleEvidence(BaseModel):
    """内部記事に対するreviewerの精査済み採用1件。claimを持つ。"""

    model_config = ConfigDict(frozen=True)

    option_index: int = Field(ge=0)
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
        option_index: int,
        task_index: int,
    ) -> Self:
        return cls(
            option_index=option_index,
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

    option_index: int = Field(ge=0)
    task_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1,
        max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS,
    )
    url: SafeUrl
    title: str = Field(min_length=1)
    content: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None

    @classmethod
    def from_reviewed_hit(
        cls,
        hit: ExternalSearchHit,
        *,
        selection: EvidenceReviewerSelection,
        option_index: int,
        task_index: int,
    ) -> Self:
        return cls(
            option_index=option_index,
            task_index=task_index,
            claim=selection.claim,
            why_selected=selection.why_selected,
            url=hit.url,
            title=hit.title,
            content=hit.content,
            published_at=hit.published_at,
            source_name=hit.source_name,
        )


class AnswerEvidence(BaseModel):
    """見せた番号から復元し、件数制限を終えた回答用Evidence。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    internal_evidence: tuple[InternalArticleEvidence, ...] = ()
    external_evidence: tuple[ExternalSearchEvidence, ...] = ()

    @classmethod
    def from_reviewer_response(
        cls,
        *,
        preparation: EvidenceReviewPreparation,
        reviewer_response: EvidenceReviewerResponse,
    ) -> AnswerEvidence:
        """見せた番号の選択だけを復元する。同じindexのclaim衝突はその番号を不採用にする。"""
        selection_by_index: dict[int, EvidenceReviewerSelection] = {}
        unadopted_indexes: set[int] = set()
        for selection in reviewer_response.selections:
            index = selection.option_index
            if preparation.resolve_option_origin(index) is None:
                continue
            if index in unadopted_indexes:
                continue
            existing_selection = selection_by_index.get(index)
            if existing_selection is None:
                selection_by_index[index] = selection
            elif existing_selection.claim != selection.claim:
                del selection_by_index[index]
                unadopted_indexes.add(index)

        internal_evidence: list[InternalArticleEvidence] = []
        external_evidence: list[ExternalSearchEvidence] = []
        for index, selection in selection_by_index.items():
            origin = preparation.resolve_option_origin(index)
            if origin is None:
                continue
            if isinstance(origin.search_hit, InternalArticleSearchHit):
                internal_evidence.append(
                    InternalArticleEvidence.from_reviewed_hit(
                        origin.search_hit,
                        selection=selection,
                        option_index=index,
                        task_index=origin.task_index,
                    )
                )
            else:
                external_evidence.append(
                    ExternalSearchEvidence.from_reviewed_hit(
                        origin.search_hit,
                        selection=selection,
                        option_index=index,
                        task_index=origin.task_index,
                    )
                )
            if len(internal_evidence) + len(external_evidence) >= ANSWER_EVIDENCE_LIMIT:
                break

        return cls(
            internal_evidence=tuple(internal_evidence),
            external_evidence=tuple(external_evidence),
        )

    @property
    def count(self) -> int:
        return len(self.internal_evidence) + len(self.external_evidence)

    @property
    def task_indexes(self) -> set[int]:
        return {
            item.task_index
            for item in (*self.internal_evidence, *self.external_evidence)
        }

    @model_validator(mode="after")
    def _validate_answer_evidence(self) -> Self:
        if self.count > ANSWER_EVIDENCE_LIMIT:
            raise ValueError("answer evidence exceeds input limit")

        internal_source_identities = [
            (item.task_index, item.curation_id) for item in self.internal_evidence
        ]
        if len(internal_source_identities) != len(set(internal_source_identities)):
            raise ValueError(
                "internal answer evidence article must be unique within a task"
            )

        external_source_identities = [
            (item.task_index, str(item.url)) for item in self.external_evidence
        ]
        if len(external_source_identities) != len(set(external_source_identities)):
            raise ValueError(
                "external answer evidence URL must be unique within a task"
            )

        option_indexes = [item.option_index for item in self.internal_evidence] + [
            item.option_index for item in self.external_evidence
        ]
        if len(option_indexes) != len(set(option_indexes)):
            raise ValueError("answer evidence option_index must be unique")
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

    failure_code: str = Field(min_length=1, pattern=r"\S")


EvidenceRunResult = EvidenceRunCompleted | EvidenceRunFailed
