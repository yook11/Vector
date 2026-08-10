"""Evidence collection contract: engine ports と task/run 単位の outcome DTO。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.evidence_collection.evidence_review import (
    EvidenceReviewOutcome,
    InternalArticleEvidence,
)
from app.agent.evidence_collection.evidence_review.contract import (
    EVIDENCE_REVIEW_ADOPTION_LIMIT,
    EVIDENCE_REVIEW_MISSING_LIMIT,
)
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
    MISSING_ITEM_MAX_CHARS,
    ExternalSearchCandidate,
    TimeFilterFailureReason,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)

__all__ = [
    "CollectedNews",
    "CollectedTask",
    "EvidenceCollectionOutcome",
    "EvidenceReviewReport",
    "EvidenceReviewStatus",
    "ResearchTaskReport",
    "ReviewedEvidence",
    "TaskExternalCollectionStatus",
    "TaskInternalCollectionStatus",
]

# 同名の app.agent.evidence_collection.researcher.ExternalCollectionStatus は
# Researcher単体の到達状況(3値)を表す別概念であり、ここは task report の
# 最終的な4値診断のため名前を分けて衝突を避ける。
TaskInternalCollectionStatus = Literal["succeeded", "failed"]
TaskExternalCollectionStatus = Literal[
    "succeeded",
    "query_generation_failed",
    "provider_failed",
    "time_filter_failed",
]
EvidenceReviewStatus = Literal["succeeded", "failed", "skipped_empty"]


class ResearchTaskReport(BaseModel):
    """task 単位の収集(内部/外部)の実行内容・失敗分類。

    精査結果はEvidenceReviewReportへ分離した。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_index: int = Field(ge=0)
    research_goal: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1),
    ]
    internal_collection: TaskInternalCollectionStatus
    external_collection: TaskExternalCollectionStatus
    time_filter_failure_reason: TimeFilterFailureReason | None = None
    generated_queries: list[str] = Field(default_factory=list)
    provider_failed_query_count: int = Field(default=0, ge=0)
    internal_candidate_count: int = Field(default=0, ge=0)
    external_candidate_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_report(self) -> Self:
        if self.external_collection == "time_filter_failed":
            if self.time_filter_failure_reason is None:
                raise ValueError("time_filter_failed requires a failure reason")
            if (
                self.generated_queries
                or self.provider_failed_query_count != 0
                or self.external_candidate_count != 0
            ):
                raise ValueError(
                    "time_filter_failed must keep external diagnostics closed"
                )
        elif self.time_filter_failure_reason is not None:
            raise ValueError("time filter failure reason requires time_filter_failed")

        if self.external_collection == "query_generation_failed" and (
            self.generated_queries
            or self.provider_failed_query_count != 0
            or self.external_candidate_count != 0
        ):
            raise ValueError(
                "query_generation_failed must keep external diagnostics closed"
            )

        if self.external_collection == "provider_failed" and (
            not self.generated_queries
            or self.provider_failed_query_count != len(self.generated_queries)
            or self.external_candidate_count != 0
        ):
            raise ValueError(
                "provider_failed requires every generated query to have failed"
            )

        if len(self.generated_queries) > EXTERNAL_TASK_QUERY_LIMIT:
            raise ValueError("generated queries exceed external query limit")
        if any(
            len(query) > EXTERNAL_QUERY_MAX_CHARS for query in self.generated_queries
        ):
            raise ValueError("generated query exceeds max length")

        return self


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
class CollectedTask:
    """1 taskのResearcher収集結果と収集onlyのreport。精査はRun単位で別途行う。"""

    task_index: int
    research_goal: str
    internal_hits: list[InternalArticleSearchHit]
    external_candidates: list[ExternalSearchCandidate]
    executed_queries: tuple[str, ...]
    report: ResearchTaskReport


@dataclass(frozen=True, slots=True)
class CollectedNews:
    """全task並列収集の結果。Run単位1回の精査(evidence review)の入力になる。"""

    tasks: list[CollectedTask]
    requested_agent_count: int | None
    effective_agent_count: int

    @property
    def has_candidates(self) -> bool:
        return any(
            task.internal_hits or task.external_candidates for task in self.tasks
        )

    @property
    def executed_queries_by_task(self) -> dict[int, tuple[str, ...]]:
        return {task.task_index: task.executed_queries for task in self.tasks}


@dataclass(frozen=True, slots=True)
class ReviewedEvidence:
    """Run単位精査の確定結果。review_outcomeは精査が成功した場合のみ持つ。"""

    outcome: EvidenceCollectionOutcome
    review_outcome: EvidenceReviewOutcome | None
