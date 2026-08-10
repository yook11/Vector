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
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.contract import (
    EVIDENCE_REVIEW_ADOPTION_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
)
from app.agent.evidence_collection.contract import ResearchTaskReport
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
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
    "EvidenceCollectionOutcome",
    "EvidenceReviewDraft",
    "EvidenceReviewInput",
    "EvidenceReviewOutcome",
    "EvidenceReviewReport",
    "EvidenceReviewResult",
    "EvidenceReviewStatus",
    "EvidenceReviewTaskGroup",
    "InternalArticleEvidence",
    "ReviewSelection",
    "ReviewSelectionDraft",
    "ReviewTaskCandidates",
    "ReviewedEvidence",
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


EvidenceReviewStatus = Literal["succeeded", "failed", "skipped_empty"]


class EvidenceReviewReport(BaseModel):
    """Run 単位の精査(採用/不足)の実行内容・失敗分類。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review: EvidenceReviewStatus
    review_failure_reason: str | None = None
    internal_evidence_count: int = Field(default=0, ge=0)
    external_evidence_count: int = Field(default=0, ge=0)
    dropped_selection_count: int = Field(default=0, ge=0)
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_review_report(self) -> Self:
        if self.review == "skipped_empty" and (
            self.internal_evidence_count != 0
            or self.external_evidence_count != 0
            or self.dropped_selection_count != 0
            or self.missing
            or self.review_failure_reason is not None
        ):
            raise ValueError("skipped_empty review must keep diagnostics closed")

        if self.review == "failed":
            if self.internal_evidence_count != 0 or self.external_evidence_count != 0:
                raise ValueError("failed review must report zero evidence")
            if self.review_failure_reason is None:
                raise ValueError("failed review requires a failure reason")
        elif self.review_failure_reason is not None:
            raise ValueError("review_failure_reason is only valid when review failed")

        if len(self.missing) > EVIDENCE_REVIEW_MISSING_LIMIT:
            raise ValueError("missing exceeds missing limit")
        if any(len(item) > MISSING_ITEM_MAX_CHARS for item in self.missing):
            raise ValueError("missing item exceeds max length")
        if (
            self.internal_evidence_count + self.external_evidence_count
            > EVIDENCE_REVIEW_ADOPTION_LIMIT
        ):
            raise ValueError("evidence count exceeds adoption cap")
        return self


class EvidenceCollectionOutcome(BaseModel):
    """plan 実行の純粋な結果。task 単位の収集reportとRun単位の精査reportを持つ。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    internal_evidence: list[InternalArticleEvidence] = Field(default_factory=list)
    internal_deduplicated_count: int = Field(default=0, ge=0)
    external_search: ExternalSearchOutcome | None = None
    task_reports: list[ResearchTaskReport] = Field(min_length=1)
    review: EvidenceReviewReport

    @model_validator(mode="after")
    def _validate_task_reports(self) -> Self:
        report_indexes = {report.task_index for report in self.task_reports}
        if report_indexes != set(range(len(self.task_reports))):
            raise ValueError("task reports must cover each task index exactly once")

        external_evidence = (
            self.external_search.evidence if self.external_search is not None else []
        )
        evidence_task_indexes = {item.task_index for item in self.internal_evidence}
        evidence_task_indexes |= {item.task_index for item in external_evidence}
        if not evidence_task_indexes <= report_indexes:
            raise ValueError("evidence task_index must reference a reported task")

        source_refs = [item.source_ref for item in self.internal_evidence] + [
            item.source_ref for item in external_evidence
        ]
        if len(source_refs) != len(set(source_refs)):
            raise ValueError(
                "evidence source_ref must be unique across internal and external"
            )

        external_deduplicated_count = (
            self.external_search.deduplicated_evidence_count
            if self.external_search is not None
            else 0
        )
        if self.review.internal_evidence_count != (
            len(self.internal_evidence) + self.internal_deduplicated_count
        ):
            raise ValueError(
                "review internal evidence count must match outcome evidence"
            )
        if self.review.external_evidence_count != (
            len(external_evidence) + external_deduplicated_count
        ):
            raise ValueError(
                "review external evidence count must match outcome evidence"
            )
        return self


@dataclass(frozen=True, slots=True)
class ReviewedEvidence:
    """Run単位精査の確定結果。review_outcomeは精査が成功した場合のみ持つ。"""

    outcome: EvidenceCollectionOutcome
    review_outcome: EvidenceReviewOutcome | None
