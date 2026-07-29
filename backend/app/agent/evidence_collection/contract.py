"""Evidence collection contract: engine ports と task/run 単位の outcome DTO。"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.evidence_collection.evidence_review import InternalArticleEvidence
from app.agent.evidence_collection.evidence_review.contract import (
    EVIDENCE_REVIEW_ADOPTION_LIMIT_PER_TASK,
    EVIDENCE_REVIEW_MISSING_LIMIT_PER_TASK,
)
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
    MISSING_ITEM_MAX_CHARS,
    TimeFilterFailureReason,
)

__all__ = [
    "EvidenceCollectionOutcome",
    "ResearchTaskReport",
    "TaskExternalCollectionStatus",
    "TaskInternalCollectionStatus",
    "TaskReviewStatus",
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
TaskReviewStatus = Literal["succeeded", "failed", "skipped_empty"]


class ResearchTaskReport(BaseModel):
    """task 単位の収集(内部/外部)と選別の実行内容・失敗分類。"""

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
    review: TaskReviewStatus
    review_failure_reason: str | None = None
    internal_evidence_count: int = Field(default=0, ge=0)
    external_evidence_count: int = Field(default=0, ge=0)
    dropped_selection_count: int = Field(default=0, ge=0)
    missing: list[str] = Field(default_factory=list)

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

        if self.review == "skipped_empty" and (
            self.internal_candidate_count != 0
            or self.external_candidate_count != 0
            or self.internal_evidence_count != 0
            or self.external_evidence_count != 0
            or self.dropped_selection_count != 0
            or self.missing
        ):
            raise ValueError("skipped_empty review must keep diagnostics closed")

        if self.review == "failed":
            if self.internal_evidence_count != 0 or self.external_evidence_count != 0:
                raise ValueError("failed review must report zero evidence")
            if self.review_failure_reason is None:
                raise ValueError("failed review requires a failure reason")
        elif self.review_failure_reason is not None:
            raise ValueError("review_failure_reason is only valid when review failed")

        if len(self.missing) > EVIDENCE_REVIEW_MISSING_LIMIT_PER_TASK:
            raise ValueError("missing exceeds missing limit")
        if any(len(item) > MISSING_ITEM_MAX_CHARS for item in self.missing):
            raise ValueError("missing item exceeds max length")
        if (
            self.internal_evidence_count + self.external_evidence_count
            > EVIDENCE_REVIEW_ADOPTION_LIMIT_PER_TASK
        ):
            raise ValueError("evidence count exceeds task cap")
        return self


class EvidenceCollectionOutcome(BaseModel):
    """plan 実行の純粋な結果。task 単位 report と精査済み根拠データを持つ。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    internal_evidence: list[InternalArticleEvidence] = Field(default_factory=list)
    internal_deduplicated_count: int = Field(default=0, ge=0)
    external_search: ExternalSearchOutcome | None = None
    task_reports: list[ResearchTaskReport] = Field(min_length=1)

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
        reported_internal_count = sum(
            report.internal_evidence_count for report in self.task_reports
        )
        reported_external_count = sum(
            report.external_evidence_count for report in self.task_reports
        )
        if reported_internal_count != (
            len(self.internal_evidence) + self.internal_deduplicated_count
        ):
            raise ValueError(
                "reported internal evidence count must match outcome evidence"
            )
        if reported_external_count != (
            len(external_evidence) + external_deduplicated_count
        ):
            raise ValueError(
                "reported external evidence count must match outcome evidence"
            )
        return self
