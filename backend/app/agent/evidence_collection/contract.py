"""Evidence collection contract: 収集の status 分類・task report・Run 収集結果 DTO。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_TASK_QUERY_LIMIT,
    ExternalSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)
from app.agent.planning.contract import SearchPlan

__all__ = [
    "CollectedNews",
    "CollectedTask",
    "EvidenceCollector",
    "ResearchTaskReport",
    "TaskExternalCollectionStatus",
    "TaskInternalCollectionStatus",
]

TaskInternalCollectionStatus = Literal["succeeded", "failed"]
TaskExternalCollectionStatus = Literal[
    "succeeded",
    "query_generation_failed",
    "provider_failed",
]


class ResearchTaskReport(BaseModel):
    """task 単位の収集(内部/外部)の実行内容・失敗分類。

    精査結果はEvidence Runの成功/失敗型へ分離した。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_index: int = Field(ge=0)
    research_goal: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1),
    ]
    internal_collection: TaskInternalCollectionStatus
    external_collection: TaskExternalCollectionStatus
    generated_queries: list[str] = Field(default_factory=list)
    provider_failed_query_count: int = Field(default=0, ge=0)
    internal_hit_count: int = Field(default=0, ge=0)
    external_hit_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_report(self) -> Self:
        if self.external_collection == "query_generation_failed" and (
            self.generated_queries
            or self.provider_failed_query_count != 0
            or self.external_hit_count != 0
        ):
            raise ValueError(
                "query_generation_failed must keep external diagnostics closed"
            )

        if self.external_collection == "provider_failed" and (
            not self.generated_queries
            or self.provider_failed_query_count != len(self.generated_queries)
            or self.external_hit_count != 0
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
    """1 taskの収集結果と収集onlyのreport。精査はRun単位で別途行う。"""

    task_index: int
    research_goal: str
    internal_hits: list[InternalArticleSearchHit]
    external_hits: list[ExternalSearchHit]
    executed_queries: tuple[str, ...]
    report: ResearchTaskReport


@dataclass(frozen=True, slots=True)
class CollectedNews:
    """全task並列収集の結果。Run単位1回の精査(evidence review)の入力になる。"""

    tasks: list[CollectedTask]

    def __post_init__(self) -> None:
        task_indexes = {task.task_index for task in self.tasks}
        if task_indexes != set(range(len(self.tasks))):
            raise ValueError("collected tasks must cover each task index exactly once")
        if any(task.report.task_index != task.task_index for task in self.tasks):
            raise ValueError("collected task and report task_index must match")

    @property
    def has_hits(self) -> bool:
        return any(task.internal_hits or task.external_hits for task in self.tasks)

    @property
    def executed_queries_by_task(self) -> dict[int, tuple[str, ...]]:
        return {task.task_index: task.executed_queries for task in self.tasks}


class EvidenceCollector(Protocol):
    """Run単位の収集を外へ公開する契約。資源のscopeは実装が閉じる。"""

    async def collect(
        self,
        *,
        plan: SearchPlan,
        as_of: datetime,
    ) -> CollectedNews: ...
