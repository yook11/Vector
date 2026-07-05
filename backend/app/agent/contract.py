"""Agent core の最小入出力 contract。

API / UI / graph runtime から独立した final result の型だけをここで保証する。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.shared.security.safe_url import SafeUrl

if TYPE_CHECKING:
    from app.agent.planning.plan_draft import QuestionPlanDraft

__all__ = [
    "AnswerExecutionSummary",
    "AnswerQuestionInput",
    "AnswerQuestionResult",
    "AnswerRetrievalSummary",
    "EXTERNAL_RESEARCH_TASK_LIMIT",
    "AnswerSource",
    "ExecutionRoute",
    "ExternalResearchTask",
    "ExternalUrlSource",
    "InternalArticleSource",
    "QuestionAnsweringAgent",
    "QuestionPlan",
    "RetrievalMode",
    "UnmetRequirement",
]

RetrievalMode = Literal["none", "internal", "external", "internal_and_external"]
ExecutionRoute = Literal[
    "direct",
    "internal",
    "external_search",
    "internal_and_external",
    "workers",
]
UnmetRequirement = Literal["internal_retrieval", "external_search"]

EXTERNAL_RESEARCH_TASK_LIMIT = 3


class AnswerQuestionInput(BaseModel):
    """ユーザー質問と実行基準時刻を agent core に渡す入力。"""

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1)
    as_of: datetime


class ExternalResearchTask(BaseModel):
    """外部リサーチの実行単位。planner は調査目的だけを言語化する。"""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    collection_goal: str = Field(min_length=1)


class QuestionPlan(BaseModel):
    """Planner が agent 内部へ返す完成済み plan。"""

    model_config = ConfigDict(frozen=True)

    retrieval_mode: RetrievalMode
    internal_queries: list[str] = Field(default_factory=list)
    external_research_tasks: list[ExternalResearchTask] = Field(default_factory=list)
    target_time_window: str | None = None
    reason: str = Field(min_length=1)

    @classmethod
    def from_draft(
        cls,
        draft: QuestionPlanDraft,
        *,
        fallback_query: str,
    ) -> Self:
        """LLM draft を完成済み plan に整える。"""

        match draft.retrieval_mode:
            case "none":
                return cls(
                    retrieval_mode="none",
                    internal_queries=[],
                    external_research_tasks=[],
                    target_time_window=draft.target_time_window,
                    reason=draft.reason,
                )
            case "internal":
                return cls(
                    retrieval_mode="internal",
                    internal_queries=_clean_plan_queries(draft.internal_queries)
                    or [fallback_query],
                    external_research_tasks=[],
                    target_time_window=draft.target_time_window,
                    reason=draft.reason,
                )
            case "external":
                return cls(
                    retrieval_mode="external",
                    internal_queries=[],
                    external_research_tasks=_clean_external_research_tasks(
                        draft.external_collection_goals
                    )
                    or [_default_external_research_task(fallback_query)],
                    target_time_window=draft.target_time_window,
                    reason=draft.reason,
                )
            case "internal_and_external":
                return cls(
                    retrieval_mode="internal_and_external",
                    internal_queries=_clean_plan_queries(draft.internal_queries)
                    or [fallback_query],
                    external_research_tasks=_clean_external_research_tasks(
                        draft.external_collection_goals
                    )
                    or [_default_external_research_task(fallback_query)],
                    target_time_window=draft.target_time_window,
                    reason=draft.reason,
                )

    @classmethod
    def safe_fallback(cls, *, fallback_query: str) -> Self:
        """Planner が使えない時の安全側 fallback plan。"""

        return cls(
            retrieval_mode="internal",
            internal_queries=[fallback_query],
            external_research_tasks=[],
            reason="planner output invalid; defaulted to internal retrieval",
        )

    @model_validator(mode="after")
    def _validate_completed_plan(self) -> Self:
        if not _all_queries_clean(self.internal_queries):
            raise ValueError("question plan queries must be non-empty strings")
        if len(self.external_research_tasks) > EXTERNAL_RESEARCH_TASK_LIMIT:
            raise ValueError("external research tasks exceed limit")
        if not _external_task_goals_unique(self.external_research_tasks):
            raise ValueError("external research task goals must be unique")
        match self.retrieval_mode:
            case "none":
                if self.internal_queries or self.external_research_tasks:
                    raise ValueError("none plan cannot include retrieval queries")
            case "internal":
                if not self.internal_queries or self.external_research_tasks:
                    raise ValueError("internal plan requires internal queries only")
            case "external":
                if self.internal_queries or not self.external_research_tasks:
                    raise ValueError(
                        "external plan requires external research tasks only"
                    )
            case "internal_and_external":
                if not self.internal_queries or not self.external_research_tasks:
                    raise ValueError(
                        "internal_and_external plan requires internal queries "
                        "and external research tasks"
                    )
        return self


def _clean_plan_queries(queries: list[str]) -> list[str]:
    return [cleaned for query in queries if (cleaned := query.strip())]


def _all_queries_clean(queries: list[str]) -> bool:
    return all(bool(query.strip()) for query in queries)


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


class AnswerRetrievalSummary(BaseModel):
    """planner が必要と判断した情報取得と、未充足の要件。"""

    model_config = ConfigDict(frozen=True)

    planned_mode: RetrievalMode
    unmet_requirements: list[UnmetRequirement] = Field(default_factory=list)


class AnswerExecutionSummary(BaseModel):
    """agent が実際に通った主要経路の summary。"""

    model_config = ConfigDict(frozen=True)

    route: ExecutionRoute
    used_internal_retrieval: bool
    used_external_search: bool

    @model_validator(mode="after")
    def _validate_route_consistency(self) -> Self:
        expected = {
            "direct": (False, False),
            "internal": (True, False),
            "external_search": (False, True),
            "internal_and_external": (True, True),
        }.get(self.route)
        if expected is None:
            return self
        if (self.used_internal_retrieval, self.used_external_search) != expected:
            raise ValueError(f"{self.route} route has inconsistent retrieval flags")
        return self


class InternalArticleSource(BaseModel):
    """内部分析済み記事に接地した回答 source。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["internal_article"] = "internal_article"
    source_ref: str = Field(min_length=1)
    article_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    snippet: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None


class ExternalUrlSource(BaseModel):
    """外部 URL に接地した回答 source。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["external_url"] = "external_url"
    source_ref: str = Field(min_length=1)
    url: SafeUrl
    title: str = Field(min_length=1)
    snippet: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None


AnswerSource = Annotated[
    InternalArticleSource | ExternalUrlSource,
    Field(discriminator="kind"),
]


class AnswerQuestionResult(BaseModel):
    """chat UI に変換される agent core の final result。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["answered", "insufficient"]
    answer: str = Field(min_length=1)
    sources: list[AnswerSource] = Field(default_factory=list)
    missing_aspects: list[str] = Field(default_factory=list)
    retrieval: AnswerRetrievalSummary
    execution: AnswerExecutionSummary

    @model_validator(mode="after")
    def _validate_provenance(self) -> Self:
        if self.status == "answered":
            if self.execution.route != "direct" and not self.sources:
                raise ValueError("non-direct answered result must include a source")
            if self.missing_aspects:
                raise ValueError("answered result cannot include missing aspects")
            if self.retrieval.unmet_requirements:
                raise ValueError("answered result cannot include unmet requirements")
        has_external_source = any(
            isinstance(source, ExternalUrlSource) for source in self.sources
        )
        if (
            self.status == "answered"
            and self.execution.used_external_search
            and not has_external_source
        ):
            raise ValueError(
                "answered result using external search must include "
                "an external URL source"
            )
        return self


class QuestionAnsweringAgent(Protocol):
    """agent core の最小呼び出し口。"""

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult: ...
