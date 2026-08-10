"""Evidence collection contract: 収集の status 分類・task report・Run 収集結果 DTO。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
    ExternalSearchCandidate,
    TimeFilterFailureReason,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)

__all__ = [
    "CollectedNews",
    "CollectedTask",
    "ResearchTaskReport",
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
