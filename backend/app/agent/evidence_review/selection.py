"""LLMの精査出力と、それを契約化した未確定の選択。

自由記述欄の clamp は from_raw factory で行い、Field 制約は
「factory を通れば違反しない」不変条件として保持する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agent.contract import (
    EVIDENCE_REVIEW_MISSING_LIMIT,
    MISSING_ITEM_MAX_CHARS,
)
from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EVIDENCE_WHY_SELECTED_MAX_CHARS,
)
from app.agent.evidence_review.answer_evidence import ANSWER_EVIDENCE_LIMIT

__all__ = [
    "EvidenceReviewerDraft",
    "EvidenceReviewerResponse",
    "EvidenceReviewerSelection",
    "EvidenceReviewerSelectionDraft",
]


class EvidenceReviewerSelectionDraft(BaseModel):
    """Reviewerがoption indexを参照して返すdraft 1件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    option_index: int = Field(ge=0)
    claim: str
    why_selected: str


class EvidenceReviewerDraft(BaseModel):
    """Reviewer Agentの回答内容。検証はresponse schemaのみで契約は未検証。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selections: list[EvidenceReviewerSelectionDraft]
    missing: list[str]


class EvidenceReviewerSelection(BaseModel):
    """Reviewer回答に含まれる、出典の復元前の選択1件。"""

    model_config = ConfigDict(frozen=True)

    option_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1,
        max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS,
    )


class EvidenceReviewerResponse(BaseModel):
    """Draftを検証・整形したReviewer回答。出典の復元前。"""

    model_config = ConfigDict(frozen=True)

    selections: tuple[EvidenceReviewerSelection, ...] = Field(
        default_factory=tuple,
        max_length=ANSWER_EVIDENCE_LIMIT,
    )
    missing: tuple[
        Annotated[str, StringConstraints(max_length=MISSING_ITEM_MAX_CHARS)],
        ...,
    ] = Field(
        default=(),
        max_length=EVIDENCE_REVIEW_MISSING_LIMIT,
    )

    @classmethod
    def from_draft(cls, draft: EvidenceReviewerDraft) -> EvidenceReviewerResponse:
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
        selections_list: list[EvidenceReviewerSelection] = []
        for selection in selections:
            if isinstance(selection, EvidenceReviewerSelection):
                selections_list.append(selection)
                continue
            item = dict(selection)
            if "claim" in item:
                item["claim"] = str(item["claim"])[:EVIDENCE_CLAIM_MAX_CHARS]
            if "why_selected" in item:
                item["why_selected"] = str(item["why_selected"])[
                    :EVIDENCE_WHY_SELECTED_MAX_CHARS
                ]
            selections_list.append(EvidenceReviewerSelection.model_validate(item))

        missing_list = [
            str(item)[:MISSING_ITEM_MAX_CHARS]
            for item in missing[:EVIDENCE_REVIEW_MISSING_LIMIT]
        ]
        return cls(
            selections=tuple(selections_list),
            missing=tuple(missing_list),
        )
