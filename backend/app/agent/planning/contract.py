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

from app.agent.contract import (
    MAX_ARTICLE_SEARCH_QUERIES,
    RESEARCH_GOAL_MAX_CHARS,
    RESEARCH_TASK_LIMIT,
    PlanType,
)
from app.agent.research_handoff.handoff import ResearchHandoff
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = [
    "ExternalResearchTask",
    "DirectAnswerPlan",
    "MAX_ARTICLE_SEARCH_QUERIES",
    "PlanQuery",
    "PlanningInput",
    "QuestionPlan",
    "QuestionPlanDraft",
    "QuestionPlanner",
    "PlanType",
    "RESEARCH_GOAL_MAX_CHARS",
    "RESEARCH_TASK_LIMIT",
    "ResearchTask",
    "ResearchTaskDraft",
    "SearchPlan",
    "TargetTimeWindow",
    "TargetTimeWindowKind",
    "plan_from_draft",
    "render_target_time_window",
]

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


@dataclass(frozen=True, slots=True)
class PlanningInput:
    """この工程が受け取り、Agent に渡す入力。"""

    question: str
    as_of: datetime
    # 現在の質問より前のthreadメッセージ(古い順)。
    history: tuple[ThreadMessageSnapshot, ...] = ()
    # 同threadの調査の申し送り。読出し・検証失敗時はNone。
    research_handoff: ResearchHandoff | None = None


class ResearchTaskDraft(BaseModel):
    """LLM構造化出力から素朴にparseされたtask draft(正規化前は無制約)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    research_goal: str
    article_search_queries: list[str]


class QuestionPlanDraft(BaseModel):
    """Planner-internal draft parsed from structured LLM output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_type: PlanType
    research_tasks: list[ResearchTaskDraft]
    target_time_window: TargetTimeWindow | None = None


class ExternalResearchTask(BaseModel):
    """外部リサーチの実行単位。planner は調査目的だけを言語化する。"""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    research_goal: str = Field(min_length=1)


class ResearchTask(BaseModel):
    """1つの調査目的に内部検索queryを関連づける実行単位。"""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    research_goal: str = Field(min_length=1)
    # 予算の正本はSearchPlanのvalidatorであり、これはrun全体予算に従う上限。
    article_search_queries: list[PlanQuery] = Field(
        min_length=1,
        max_length=MAX_ARTICLE_SEARCH_QUERIES,
    )

    @field_validator("article_search_queries")
    @classmethod
    def _validate_unique_queries_within_task(cls, value: list[str]) -> list[str]:
        query_keys = [query.casefold() for query in value]
        if len(query_keys) != len(set(query_keys)):
            raise ValueError("article search queries must be unique within a task")
        return value


class DirectAnswerPlan(BaseModel):
    """Completed plan for direct answer without retrieval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_type: Literal["direct_answer"] = "direct_answer"


class SearchPlan(BaseModel):
    """Completed plan that always collects internal and external evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_type: Literal["search"] = "search"
    research_tasks: list[ResearchTask] = Field(
        min_length=1,
        max_length=RESEARCH_TASK_LIMIT,
    )
    target_time_window: TargetTimeWindow | None = None

    @model_validator(mode="after")
    def _validate_research_tasks(self) -> Self:
        goals = [task.research_goal for task in self.research_tasks]
        if len(goals) != len(set(goals)):
            raise ValueError("research task goals must be unique")
        total_queries = sum(
            len(task.article_search_queries) for task in self.research_tasks
        )
        if total_queries > MAX_ARTICLE_SEARCH_QUERIES:
            raise ValueError("article search query total exceeds budget")
        return self


QuestionPlan = DirectAnswerPlan | SearchPlan


class QuestionPlanner(Protocol):
    """Planner boundary that returns a completed ``QuestionPlan``."""

    async def plan(self, input: PlanningInput) -> QuestionPlan: ...


def plan_from_draft(
    draft: QuestionPlanDraft,
) -> QuestionPlan:
    """LLM draft を完成済み plan に整える。"""

    cleaned_tasks = _clean_research_tasks(draft.research_tasks)
    if draft.plan_type == "direct_answer":
        if cleaned_tasks or draft.target_time_window is not None:
            raise _response_defect()
        return DirectAnswerPlan()
    if not cleaned_tasks or any(not queries for _, queries in cleaned_tasks):
        raise _response_defect()
    trimmed_tasks = _trim_query_budget(cleaned_tasks)
    return SearchPlan(
        research_tasks=[
            ResearchTask(research_goal=goal, article_search_queries=queries)
            for goal, queries in trimmed_tasks
        ],
        target_time_window=draft.target_time_window,
    )


def _clean_research_tasks(
    tasks: list[ResearchTaskDraft],
) -> list[tuple[str, list[str]]]:
    cleaned_tasks: list[tuple[str, list[str]]] = []
    seen_goals: set[str] = set()
    for task in tasks:
        research_goal = task.research_goal.strip()[:RESEARCH_GOAL_MAX_CHARS]
        if not research_goal or research_goal in seen_goals:
            continue
        cleaned_tasks.append(
            (research_goal, _clean_task_queries(task.article_search_queries))
        )
        seen_goals.add(research_goal)
        if len(cleaned_tasks) >= RESEARCH_TASK_LIMIT:
            break
    return cleaned_tasks


def _clean_task_queries(queries: list[str]) -> list[str]:
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
    return cleaned_queries


def _trim_query_budget(
    tasks: list[tuple[str, list[str]]],
) -> list[tuple[str, list[str]]]:
    """予算超過時のみ、query位置ごとにtask順で走査し予算件数で決定的に打ち切る。"""
    total = sum(len(queries) for _, queries in tasks)
    if total <= MAX_ARTICLE_SEARCH_QUERIES:
        return tasks
    trimmed_queries: list[list[str]] = [[] for _ in tasks]
    selected = 0
    max_task_length = max((len(queries) for _, queries in tasks), default=0)
    position = 0
    while position < max_task_length and selected < MAX_ARTICLE_SEARCH_QUERIES:
        for task_index, (_, queries) in enumerate(tasks):
            if selected >= MAX_ARTICLE_SEARCH_QUERIES:
                break
            if position < len(queries):
                trimmed_queries[task_index].append(queries[position])
                selected += 1
        position += 1
    return [
        (goal, queries)
        for (goal, _), queries in zip(tasks, trimmed_queries, strict=True)
    ]


def _response_defect() -> AgentResponseInvalidError:
    return AgentResponseInvalidError(
        AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH,
        repair_hint="plan fields are inconsistent",
    )
