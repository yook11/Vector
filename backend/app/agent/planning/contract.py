"""Question planning contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal, Protocol, Self, assert_never

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

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
    "PlanningAttemptInput",
    "PlanningRequest",
    "QuestionPlan",
    "QuestionPlanDraft",
    "QuestionPlanner",
    "RetrievalPlan",
    "TargetTimeWindow",
    "TargetTimeWindowKind",
    "plan_from_draft",
    "render_target_time_window",
    "safe_fallback_plan",
]

EXTERNAL_RESEARCH_TASK_LIMIT = 3
MAX_INTERNAL_QUERIES = 3

PlanQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]

TargetTimeWindowKind = Literal[
    "today",
    "yesterday",
    "last_n_days",
    "this_week",
    "last_week",
    "this_month",
    "calendar_month",
    "date_range",
    "unsupported_explicit_window",
]


class TargetTimeWindow(BaseModel):
    """外部根拠へ適用するpublication期間の型付きplanner契約。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: TargetTimeWindowKind
    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    days: int | None = Field(default=None, ge=1, le=60)
    start_date: date | None = None
    end_date_inclusive: date | None = None

    @field_validator("start_date", "end_date_inclusive", mode="before")
    @classmethod
    def _validate_iso_date(cls, value: object) -> object:
        if value is None or type(value) is date:
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                pass
            else:
                if value == parsed.isoformat():
                    return parsed
        raise ValueError("explicit dates must use ISO YYYY-MM-DD")

    @model_validator(mode="after")
    def _validate_kind_parameters(self) -> Self:
        if self.kind == "calendar_month":
            if self.year is None or self.month is None:
                raise ValueError("calendar_month requires year and month")
        elif self.year is not None or self.month is not None:
            raise ValueError("year and month are only valid for calendar_month")

        if self.kind == "last_n_days":
            if self.days is None:
                raise ValueError("last_n_days requires days")
        elif self.days is not None:
            raise ValueError("days is only valid for last_n_days")

        if self.kind == "date_range":
            if self.start_date is None or self.end_date_inclusive is None:
                raise ValueError("date_range requires both dates")
            if self.start_date > self.end_date_inclusive:
                raise ValueError("date_range start must not exceed end")
            if self.end_date_inclusive == date.max:
                raise ValueError("date_range end must have a representable next day")
        elif self.start_date is not None or self.end_date_inclusive is not None:
            raise ValueError("explicit dates are only valid for date_range")
        return self


def render_target_time_window(target_time_window: TargetTimeWindow) -> str:
    """型付きpublication期間をprompt用の決定的な日本語へ変換する。"""

    match target_time_window.kind:
        case "today":
            return "今日"
        case "yesterday":
            return "昨日"
        case "last_n_days":
            days = target_time_window.days
            if days is None:
                raise ValueError("last_n_days requires days")
            if days == 1:
                return "直近24時間"
            return f"直近{days}日"
        case "this_week":
            return "今週"
        case "last_week":
            return "先週"
        case "this_month":
            return "今月"
        case "calendar_month":
            year = target_time_window.year
            month = target_time_window.month
            if year is None or month is None:
                raise ValueError("calendar_month requires year and month")
            return f"{year}年{month}月"
        case "date_range":
            start = target_time_window.start_date
            end = target_time_window.end_date_inclusive
            if start is None or end is None:
                raise ValueError("date_range requires both dates")
            return (
                f"{start.year}年{start.month}月{start.day}日から"
                f"{end.year}年{end.month}月{end.day}日まで"
            )
        case "unsupported_explicit_window":
            return "対応外の明示期間"
    assert_never(target_time_window.kind)


class PlanningRequest(BaseModel):
    """Plannerへ渡す質問コンテキストと実行時点。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: QuestionContext
    as_of: datetime


@dataclass(frozen=True, slots=True)
class PlanningAttemptInput:
    """Plannerの1 attemptに渡す実行時input。"""

    request: PlanningRequest
    previous_error: str | None = None


class QuestionPlanDraft(BaseModel):
    """Planner-internal draft parsed from structured LLM output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retrieval_mode: RetrievalMode
    internal_queries: list[str] = Field(default_factory=list)
    external_collection_goals: list[str] = Field(default_factory=list)
    target_time_window: TargetTimeWindow | None = None
    reason: str = Field(min_length=1)


class ExternalResearchTask(BaseModel):
    """外部リサーチの実行単位。planner は調査目的だけを言語化する。"""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    research_goal: str = Field(min_length=1)


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
    target_time_window: TargetTimeWindow | None = None
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
    target_time_window: TargetTimeWindow | None = None
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
        research_goal = goal.strip()
        if not research_goal or research_goal in seen_goals:
            continue
        cleaned_tasks.append(ExternalResearchTask(research_goal=research_goal))
        seen_goals.add(research_goal)
        if len(cleaned_tasks) >= EXTERNAL_RESEARCH_TASK_LIMIT:
            break
    return cleaned_tasks


def _default_external_research_task(fallback_query: str) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=fallback_query)


def _external_task_goals_unique(tasks: list[ExternalResearchTask]) -> bool:
    goals = [task.research_goal for task in tasks]
    return len(goals) == len(set(goals))
