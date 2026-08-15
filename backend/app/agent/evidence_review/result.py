"""精査の出力後。LLM draftの契約化、回答用根拠の復元、Runの確定を置く。

自由記述欄の clamp は from_raw factory で行い、model validator は
「factory を通れば違反しない」不変条件として保持する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.contract import (
    ANSWER_EVIDENCE_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
    EVIDENCE_REVIEWER_SELECTION_LIMIT,
)
from app.agent.evidence_collection.contract import ResearchTaskReport
from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EVIDENCE_WHY_SELECTED_MAX_CHARS,
    EXTERNAL_SEARCH_AGENT_HARD_LIMIT,
    MISSING_ITEM_MAX_CHARS,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)
from app.agent.evidence_review.draft import EvidenceReviewDraft
from app.agent.evidence_review.preparation import EvidenceReviewPreparation

__all__ = [
    "AnswerEvidence",
    "EvidenceReviewOutcome",
    "EvidenceReviewReport",
    "EvidenceReviewStatus",
    "EvidenceReviewerResponse",
    "EvidenceReviewerSelection",
    "InternalArticleEvidence",
    "ReviewedEvidence",
    "RunReviewResult",
]


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
                item["claim"] = _truncate_text(item["claim"], EVIDENCE_CLAIM_MAX_CHARS)
            if "why_selected" in item:
                item["why_selected"] = _truncate_text(
                    item["why_selected"],
                    EVIDENCE_WHY_SELECTED_MAX_CHARS,
                )
            clamped_selections.append(EvidenceReviewerSelection.model_validate(item))

        return cls(
            selections=tuple(clamped_selections),
            missing=_clamp_missing(missing),
        )

    @model_validator(mode="after")
    def _validate_missing_caps(self) -> EvidenceReviewerResponse:
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
        seen_curation_ids: set[int] = set()
        seen_urls: set[str] = set()

        for selection in reviewer_response.selections:
            index = selection.candidate_index
            entry = preparation.resolve_candidate(index)
            if entry is None or index in selected_indexes:
                continue

            selected_indexes.add(index)
            source_ref = f"{entry.task_index}-{index}"
            if isinstance(entry.source, InternalArticleSearchHit):
                curation_id = entry.source.article.curation_id
                if curation_id in seen_curation_ids:
                    continue
                seen_curation_ids.add(curation_id)
                internal_articles.append(
                    _build_internal_evidence(
                        hit=entry.source,
                        selection=selection,
                        source_ref=source_ref,
                        task_index=entry.task_index,
                    )
                )
            else:
                url = str(entry.source.url)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                external_sources.append(
                    _build_external_evidence(
                        candidate=entry.source,
                        selection=selection,
                        source_ref=source_ref,
                        task_index=entry.task_index,
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

    @model_validator(mode="after")
    def _validate_answer_evidence(self) -> Self:
        if self.count > ANSWER_EVIDENCE_LIMIT:
            raise ValueError("answer evidence exceeds input limit")

        curation_ids = [item.curation_id for item in self.internal_articles]
        if len(curation_ids) != len(set(curation_ids)):
            raise ValueError("internal answer evidence curation_id must be unique")

        urls = [str(item.url) for item in self.external_sources]
        if len(urls) != len(set(urls)):
            raise ValueError("external answer evidence URL must be unique")

        source_refs = [item.source_ref for item in self.internal_articles] + [
            item.source_ref for item in self.external_sources
        ]
        if len(source_refs) != len(set(source_refs)):
            raise ValueError("answer evidence source_ref must be unique")
        return self


def _build_internal_evidence(
    *,
    hit: InternalArticleSearchHit,
    selection: EvidenceReviewerSelection,
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
    selection: EvidenceReviewerSelection,
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


@dataclass(frozen=True, slots=True)
class EvidenceReviewOutcome:
    """EvidenceReviewer.review()が返す、回答用Evidenceを含むRun全体の精査結果。"""

    answer_evidence: AnswerEvidence
    missing: tuple[str, ...]
    failure_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing", tuple(self.missing))
        if self.failure_reason is not None and (
            self.answer_evidence.count > 0 or self.missing
        ):
            raise ValueError(
                "failed review outcome must keep evidence and missing empty"
            )


def _clamp_missing(missing: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        _truncate_text(item, MISSING_ITEM_MAX_CHARS)
        for item in missing[:EVIDENCE_REVIEW_MISSING_LIMIT]
    )


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
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_review_report(self) -> Self:
        if self.review == "skipped_empty" and (
            self.internal_evidence_count != 0
            or self.external_evidence_count != 0
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
            > ANSWER_EVIDENCE_LIMIT
        ):
            raise ValueError("evidence count exceeds answer evidence limit")
        return self


class ReviewedEvidence(BaseModel):
    """精査を経て確定した採用根拠。収集/精査それぞれのreportを併せ持つ。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_evidence: AnswerEvidence = Field(default_factory=AnswerEvidence)
    requested_external_agent_count: int | None = None
    effective_external_agent_count: int = Field(default=0, ge=0)
    external_agent_hard_limit: int = Field(
        default=EXTERNAL_SEARCH_AGENT_HARD_LIMIT,
        ge=1,
    )
    task_reports: list[ResearchTaskReport] = Field(min_length=1)
    review: EvidenceReviewReport

    @model_validator(mode="after")
    def _validate_task_reports(self) -> Self:
        report_indexes = {report.task_index for report in self.task_reports}
        if report_indexes != set(range(len(self.task_reports))):
            raise ValueError("task reports must cover each task index exactly once")

        evidence_task_indexes = {
            item.task_index for item in self.answer_evidence.internal_articles
        }
        evidence_task_indexes |= {
            item.task_index for item in self.answer_evidence.external_sources
        }
        if not evidence_task_indexes <= report_indexes:
            raise ValueError("evidence task_index must reference a reported task")

        if self.review.internal_evidence_count != len(
            self.answer_evidence.internal_articles
        ):
            raise ValueError(
                "review internal evidence count must match outcome evidence"
            )
        if self.review.external_evidence_count != len(
            self.answer_evidence.external_sources
        ):
            raise ValueError(
                "review external evidence count must match outcome evidence"
            )
        return self


@dataclass(frozen=True, slots=True)
class RunReviewResult:
    """Run単位精査の確定結果。review_outcomeは精査が成功した場合のみ持つ。"""

    evidence: ReviewedEvidence
    review_outcome: EvidenceReviewOutcome | None
