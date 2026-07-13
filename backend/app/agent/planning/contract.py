"""Question planning contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self, assert_never

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.agent.contract import RetrievalMode
from app.agent.question_context.contract import QuestionContext

__all__ = [
    "EXTERNAL_RESEARCH_TASK_LIMIT",
    "ExternalResearchTask",
    "ExternalSearchPlan",
    "InternalAndExternalPlan",
    "InternalRetrievalPlan",
    "MAX_INTERNAL_QUERIES",
    "NoRetrievalPlan",
    "PlanQuery",
    "PlanningRequest",
    "QuestionPlan",
    "QuestionPlanDraft",
    "QuestionPlanDraftGenerator",
    "QuestionPlanner",
    "QuestionPlannerResponseInvalidError",
    "RetrievalPlan",
    "plan_from_draft",
    "safe_fallback_plan",
]

EXTERNAL_RESEARCH_TASK_LIMIT = 3
MAX_INTERNAL_QUERIES = 3

PlanQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class PlanningRequest(BaseModel):
    """Plannerへ渡す質問コンテキストと実行時点。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: QuestionContext
    as_of: datetime


class QuestionPlannerResponseInvalidError(ValueError):
    """Planner response が ``QuestionPlanDraft`` として消化できない。"""

    def __init__(self, defect: StrEnum) -> None:
        self.defect = defect
        super().__init__(defect.value)


class QuestionPlanDraft(BaseModel):
    """Planner-internal draft parsed from structured LLM output."""

    model_config = ConfigDict(frozen=True)

    retrieval_mode: RetrievalMode
    internal_queries: list[str] = Field(default_factory=list)
    external_collection_goals: list[str] = Field(default_factory=list)
    target_time_window: str | None = None
    reason: str = Field(min_length=1)


class QuestionPlanDraftGenerator(Protocol):
    """LLM adapter boundary that returns draft plans."""

    async def plan(
        self,
        request: PlanningRequest,
        *,
        previous_error: str | None = None,
    ) -> QuestionPlanDraft: ...


class ExternalResearchTask(BaseModel):
    """外部リサーチの実行単位。planner は調査目的だけを言語化する。"""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    collection_goal: str = Field(min_length=1)


class NoRetrievalPlan(BaseModel):
    """Completed plan for direct answer without retrieval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retrieval_mode: Literal["none"] = "none"
    reason: str = Field(min_length=1)


class InternalRetrievalPlan(BaseModel):
    """Completed plan for internal article retrieval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retrieval_mode: Literal["internal"] = "internal"
    internal_queries: list[PlanQuery] = Field(
        min_length=1,
        max_length=MAX_INTERNAL_QUERIES,
    )
    reason: str = Field(min_length=1)


class ExternalSearchPlan(BaseModel):
    """Completed plan for external search."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retrieval_mode: Literal["external"] = "external"
    external_research_tasks: list[ExternalResearchTask] = Field(
        min_length=1,
        max_length=EXTERNAL_RESEARCH_TASK_LIMIT,
    )
    target_time_window: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_task_goals(self) -> Self:
        if not _external_task_goals_unique(self.external_research_tasks):
            raise ValueError("external research task goals must be unique")
        return self


class InternalAndExternalPlan(BaseModel):
    """Completed plan for internal retrieval plus external search."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retrieval_mode: Literal["internal_and_external"] = "internal_and_external"
    internal_queries: list[PlanQuery] = Field(
        min_length=1,
        max_length=MAX_INTERNAL_QUERIES,
    )
    external_research_tasks: list[ExternalResearchTask] = Field(
        min_length=1,
        max_length=EXTERNAL_RESEARCH_TASK_LIMIT,
    )
    target_time_window: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_task_goals(self) -> Self:
        if not _external_task_goals_unique(self.external_research_tasks):
            raise ValueError("external research task goals must be unique")
        return self


# 回答の根拠を取りに行く plan。取りに行かない NoRetrievalPlan と対をなす。
type RetrievalPlan = (
    InternalRetrievalPlan | ExternalSearchPlan | InternalAndExternalPlan
)
type QuestionPlan = NoRetrievalPlan | RetrievalPlan


class QuestionPlanner(Protocol):
    """Planner boundary that returns a completed ``QuestionPlan``."""

    async def plan(self, request: PlanningRequest) -> QuestionPlan: ...


def plan_from_draft(
    draft: QuestionPlanDraft,
    *,
    fallback_query: str,
) -> QuestionPlan:
    """LLM draft を完成済み plan に整える。"""

    match draft.retrieval_mode:
        case "none":
            return NoRetrievalPlan(reason=draft.reason)
        case "internal":
            return InternalRetrievalPlan(
                internal_queries=_clean_plan_queries(draft.internal_queries)
                or [fallback_query],
                reason=draft.reason,
            )
        case "external":
            return ExternalSearchPlan(
                external_research_tasks=_clean_external_research_tasks(
                    draft.external_collection_goals
                )
                or [_default_external_research_task(fallback_query)],
                target_time_window=draft.target_time_window,
                reason=draft.reason,
            )
        case "internal_and_external":
            return InternalAndExternalPlan(
                internal_queries=_clean_plan_queries(draft.internal_queries)
                or [fallback_query],
                external_research_tasks=_clean_external_research_tasks(
                    draft.external_collection_goals
                )
                or [_default_external_research_task(fallback_query)],
                target_time_window=draft.target_time_window,
                reason=draft.reason,
            )
    assert_never(draft.retrieval_mode)


def safe_fallback_plan(*, fallback_query: str) -> InternalRetrievalPlan:
    """Planner が使えない時の安全側 fallback plan。"""

    return InternalRetrievalPlan(
        internal_queries=[fallback_query],
        reason="planner output invalid; defaulted to internal retrieval",
    )


def _clean_plan_queries(queries: list[str]) -> list[str]:
    cleaned_queries: list[str] = []
    seen_queries: set[str] = set()
    for query in queries:
        cleaned = query.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen_queries:
            continue
        cleaned_queries.append(cleaned)
        seen_queries.add(key)
        if len(cleaned_queries) >= MAX_INTERNAL_QUERIES:
            break
    return cleaned_queries


def _clean_external_research_tasks(goals: list[str]) -> list[ExternalResearchTask]:
    cleaned_tasks: list[ExternalResearchTask] = []
    seen_goals: set[str] = set()
    for goal in goals:
        collection_goal = goal.strip()
        if not collection_goal or collection_goal in seen_goals:
            continue
        cleaned_tasks.append(ExternalResearchTask(collection_goal=collection_goal))
        seen_goals.add(collection_goal)
        if len(cleaned_tasks) >= EXTERNAL_RESEARCH_TASK_LIMIT:
            break
    return cleaned_tasks


def _default_external_research_task(fallback_query: str) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=fallback_query)


def _external_task_goals_unique(tasks: list[ExternalResearchTask]) -> bool:
    goals = [task.collection_goal for task in tasks]
    return len(goals) == len(set(goals))
