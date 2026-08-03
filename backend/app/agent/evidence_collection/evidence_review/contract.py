"""Evidence Reviewer の境界型・port 契約。

内部候補と外部候補を1つの候補列として受け取り、精査して根拠を見極める
Evidence Reviewer の入出力型・cap 定数を保証する。自由記述欄の clamp は
from_raw factory で行い、model validator は「factory を通れば違反しない」
不変条件として保持する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.contract import (
    EVIDENCE_REVIEW_ADOPTION_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
)
from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EVIDENCE_WHY_SELECTED_MAX_CHARS,
    MISSING_ITEM_MAX_CHARS,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)

__all__ = [
    "EVIDENCE_REVIEW_ADOPTION_LIMIT",
    "EVIDENCE_REVIEW_MISSING_LIMIT",
    "EvidenceCandidateInput",
    "EvidenceReviewDraft",
    "EvidenceReviewInput",
    "EvidenceReviewOutcome",
    "EvidenceReviewResult",
    "EvidenceReviewTaskGroup",
    "InternalArticleEvidence",
    "ReviewSelection",
    "ReviewSelectionDraft",
    "ReviewTaskCandidates",
]


@dataclass(frozen=True, slots=True)
class EvidenceCandidateInput:
    """Reviewerへ渡す内外統合candidate projection。URLを含まない。"""

    index: int
    title: str
    source_name: str | None
    published_at: datetime | None
    snippet: str | None


@dataclass(frozen=True, slots=True)
class ReviewTaskCandidates:
    """1 taskの精査前候補。EvidenceReviewer.review()がtask単位で受け取る入力。"""

    task_index: int
    research_goal: str
    internal_hits: list[InternalArticleSearchHit]
    external_candidates: list[ExternalSearchCandidate]


@dataclass(frozen=True, slots=True)
class EvidenceReviewTaskGroup:
    """Reviewerへ渡す、1 task分のgoalとcandidate projection。"""

    task_index: int
    research_goal: str
    candidates: tuple[EvidenceCandidateInput, ...]


@dataclass(frozen=True, slots=True)
class EvidenceReviewInput:
    """Evidence Reviewer AgentのRun単位1 attempt入力。"""

    task_groups: tuple[EvidenceReviewTaskGroup, ...]
    as_of: datetime


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


class ReviewSelection(BaseModel):
    """Reviewerが返す精査済み選別1件。URLは返さずindexでcandidateを参照する。"""

    model_config = ConfigDict(frozen=True)

    candidate_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1,
        max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS,
    )


class EvidenceReviewResult(BaseModel):
    """Reviewerの精査結果。自由記述欄のcapはfactoryで丸める。"""

    model_config = ConfigDict(frozen=True)

    selections: list[ReviewSelection] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @classmethod
    def from_raw(
        cls,
        *,
        selections: Sequence[ReviewSelection | Mapping[str, object]],
        missing: Sequence[str],
    ) -> EvidenceReviewResult:
        clamped_selections: list[ReviewSelection] = []
        for selection in selections:
            if isinstance(selection, ReviewSelection):
                clamped_selections.append(selection)
                continue
            item = dict(selection)
            if "claim" in item:
                item["claim"] = _truncate_text(item["claim"], EVIDENCE_CLAIM_MAX_CHARS)
            if "why_selected" in item:
                item["why_selected"] = _truncate_text(
                    item["why_selected"],
                    EVIDENCE_WHY_SELECTED_MAX_CHARS,
                )
            clamped_selections.append(ReviewSelection.model_validate(item))

        return cls(
            selections=clamped_selections,
            missing=_clamp_missing(missing),
        )

    @model_validator(mode="after")
    def _validate_missing_caps(self) -> EvidenceReviewResult:
        if len(self.missing) > EVIDENCE_REVIEW_MISSING_LIMIT:
            raise ValueError("missing exceeds evidence review missing limit")
        if any(len(item) > MISSING_ITEM_MAX_CHARS for item in self.missing):
            raise ValueError("missing item exceeds max length")
        return self


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


@dataclass(frozen=True, slots=True)
class EvidenceReviewOutcome:
    """EvidenceReviewer.review()が返すRun全体の精査結果。合流前の中間値。"""

    internal_evidence: list[InternalArticleEvidence]
    external_evidence: list[ExternalSearchEvidence]
    missing: list[str]
    dropped_selection_count: int
    failure_reason: str | None


def _clamp_missing(missing: Sequence[str]) -> list[str]:
    return [
        _truncate_text(item, MISSING_ITEM_MAX_CHARS)
        for item in missing[:EVIDENCE_REVIEW_MISSING_LIMIT]
    ]


def _truncate_text(value: object, max_chars: int) -> str:
    return str(value)[:max_chars]
