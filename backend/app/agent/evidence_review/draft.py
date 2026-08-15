"""Evidence Reviewer Agentの出力形。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EvidenceReviewDraft",
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
