"""LLMの精査出力と、それを契約化した未確定の選択。

自由記述欄の clamp は from_raw factory で行い、model validator は
「factory を通れば違反しない」不変条件として保持する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.contract import (
    EVIDENCE_REVIEW_MISSING_LIMIT,
    EVIDENCE_REVIEWER_SELECTION_LIMIT,
)
from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EVIDENCE_WHY_SELECTED_MAX_CHARS,
    MISSING_ITEM_MAX_CHARS,
)

__all__ = [
    "EvidenceReviewDraft",
    "EvidenceReviewerResponse",
    "EvidenceReviewerSelection",
    "ReviewSelectionDraft",
]


class ReviewSelectionDraft(BaseModel):
    """Reviewerがcandidate indexを参照して返すdraft 1件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_index: int = Field(ge=0)
    claim: str
    why_selected: str


class EvidenceReviewDraft(BaseModel):
    """Reviewerが返すsource情報を持たない精査draft。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selections: list[ReviewSelectionDraft]
    missing: list[str]


class EvidenceReviewerSelection(BaseModel):
    """Evidence Reviewerが返した、採用確定前の選択1件。"""

    model_config = ConfigDict(frozen=True)

    candidate_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1,
        max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS,
    )


class EvidenceReviewerResponse(BaseModel):
    """Evidence Reviewerが返した選択と不足事項。自由記述欄はfactoryで丸める。"""

    model_config = ConfigDict(frozen=True)

    selections: tuple[EvidenceReviewerSelection, ...] = Field(
        default_factory=tuple,
        max_length=EVIDENCE_REVIEWER_SELECTION_LIMIT,
    )
    missing: tuple[str, ...] = ()

    @classmethod
    def from_draft(cls, draft: EvidenceReviewDraft) -> EvidenceReviewerResponse:
        return cls.from_raw(
            selections=[selection.model_dump() for selection in draft.selections],
            missing=draft.missing,
        )

    @classmethod
    def from_raw(
        cls,
        *,
        selections: Sequence[EvidenceReviewerSelection | Mapping[str, object]],
        missing: Sequence[str],
    ) -> EvidenceReviewerResponse:
        clamped_selections: list[EvidenceReviewerSelection] = []
        for selection in selections:
            if isinstance(selection, EvidenceReviewerSelection):
                clamped_selections.append(selection)
                continue
            item = dict(selection)
            if "claim" in item:
                item["claim"] = cls._truncate_text(
                    item["claim"], EVIDENCE_CLAIM_MAX_CHARS
                )
            if "why_selected" in item:
                item["why_selected"] = cls._truncate_text(
                    item["why_selected"],
                    EVIDENCE_WHY_SELECTED_MAX_CHARS,
                )
            clamped_selections.append(EvidenceReviewerSelection.model_validate(item))

        return cls(
            selections=tuple(clamped_selections),
            missing=cls._clamp_missing(missing),
        )

    @classmethod
    def validate_missing(cls, missing: Sequence[str]) -> None:
        if len(missing) > EVIDENCE_REVIEW_MISSING_LIMIT:
            raise ValueError("missing exceeds evidence review missing limit")
        if any(len(item) > MISSING_ITEM_MAX_CHARS for item in missing):
            raise ValueError("missing item exceeds max length")

    @classmethod
    def _clamp_missing(cls, missing: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            cls._truncate_text(item, MISSING_ITEM_MAX_CHARS)
            for item in missing[:EVIDENCE_REVIEW_MISSING_LIMIT]
        )

    @classmethod
    def _truncate_text(cls, value: object, max_chars: int) -> str:
        return str(value)[:max_chars]

    @model_validator(mode="after")
    def _validate_missing_caps(self) -> EvidenceReviewerResponse:
        self.validate_missing(self.missing)
        return self
