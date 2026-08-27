"""Agent core の共有 contract。

API / UI / graph runtime から独立した final result と plan 型をここで保証する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Protocol, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.shared.security.safe_url import SafeUrl

__all__ = [
    "AnswerDeltaReporter",
    "AnswerGenerationContinuation",
    "AnswerGenerationStopped",
    "AnswerProgressReporter",
    "AnswerProgressEvent",
    "AnswerProgressStage",
    "AnswerQuestionResult",
    "AnswerPlanSummary",
    "AnswerEventReporter",
    "EVIDENCE_CLAIM_MAX_CHARS",
    "EVIDENCE_REVIEW_MISSING_LIMIT",
    "EXTERNAL_QUERY_MAX_CHARS",
    "EXTERNAL_TASK_QUERY_LIMIT",
    "ExternalSearchHitsFetchedEvent",
    "EvidenceReviewSelectedEvent",
    "ExternalSearchQueriesGeneratedEvent",
    "AnswerSource",
    "ExternalUrlSource",
    "InternalSearchCompletedEvent",
    "InternalSearchStartedEvent",
    "InternalArticleSource",
    "MAX_ARTICLE_SEARCH_QUERIES",
    "MISSING_ITEM_MAX_CHARS",
    "NonBlankText",
    "ORGANIZED_TEXT_MAX_CHARS",
    "PlanType",
    "RESEARCH_GOAL_MAX_CHARS",
    "RESEARCH_TASK_LIMIT",
    "ResearchHandoff",
    "ResearchRunRecord",
    "ResearchTaskRecord",
]

PlanType = Literal["direct_answer", "search"]
AnswerProgressStage = Literal[
    "safety_check",
    "context_resolution",
    "planning",
    "evidence_collection",
    "evidence_review",
    "answering",
]
NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AnswerPlanSummary(BaseModel):
    """planner が必要と判断した情報取得の種類。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_type: PlanType


class InternalArticleSource(BaseModel):
    """内部分析済み記事に接地した回答 source。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["internal_article"] = "internal_article"
    source_ref: str = Field(min_length=1)
    article_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    published_at: datetime | None = None


class ExternalUrlSource(BaseModel):
    """外部 URL に接地した回答 source。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["external_url"] = "external_url"
    source_ref: str = Field(min_length=1)
    url: SafeUrl
    title: str = Field(min_length=1)
    evidence_claim: NonBlankText
    published_at: datetime | None = None
    source_name: str | None = None


AnswerSource = Annotated[
    InternalArticleSource | ExternalUrlSource,
    Field(discriminator="kind"),
]


class InternalSearchStartedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["evidence_collection.internal_search_started"] = (
        "evidence_collection.internal_search_started"
    )
    task_index: int = Field(ge=0)
    query_count: int = Field(ge=0)


class InternalSearchCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["evidence_collection.internal_search_completed"] = (
        "evidence_collection.internal_search_completed"
    )
    task_index: int = Field(ge=0)
    hit_count: int = Field(ge=0)


class ExternalSearchQueriesGeneratedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["evidence_collection.external_search_queries_generated"] = (
        "evidence_collection.external_search_queries_generated"
    )
    task_index: int = Field(ge=0)
    queries: list[NonBlankText] = Field(default_factory=list)


class ExternalSearchHitsFetchedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["evidence_collection.external_search_hits_fetched"] = (
        "evidence_collection.external_search_hits_fetched"
    )
    task_index: int = Field(ge=0)
    hit_count: int = Field(ge=0)


class EvidenceReviewSelectedEvent(BaseModel):
    """精査はRun単位1回のため、採用件数もRun全体で1本だけ報告する。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["evidence_review.selected"] = "evidence_review.selected"
    evidence_count: int = Field(ge=0)


AnswerProgressEvent = Annotated[
    InternalSearchStartedEvent
    | InternalSearchCompletedEvent
    | ExternalSearchQueriesGeneratedEvent
    | ExternalSearchHitsFetchedEvent
    | EvidenceReviewSelectedEvent,
    Field(discriminator="type"),
]


class AnswerQuestionResult(BaseModel):
    """chat UI に変換される agent core の final result。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["answered", "insufficient"]
    answer: NonBlankText
    sources: list[AnswerSource] = Field(default_factory=list)
    missing_aspects: list[NonBlankText] = Field(default_factory=list)
    plan_summary: AnswerPlanSummary

    @model_validator(mode="after")
    def _validate_provenance(self) -> Self:
        if self.status == "answered":
            if self.plan_summary.plan_type == "search" and not self.sources:
                raise ValueError("non-direct answered result must include a source")
            if self.missing_aspects:
                raise ValueError("answered result cannot include missing aspects")
        if self.status == "insufficient" and not self.missing_aspects:
            raise ValueError("insufficient result must include missing aspects")
        if self.plan_summary.plan_type == "direct_answer" and self.sources:
            raise ValueError("direct planned result cannot include sources")
        return self


class AnswerGenerationStopped(Exception):
    """現在のrun attemptが回答生成を継続できなくなった。"""


class AnswerDeltaReporter(Protocol):
    """表示可能な回答断片をgeneration単位で通知するsink。"""

    async def append(self, *, generation: int, text: str) -> None: ...

    async def reset(self, *, generation: int) -> None: ...

    async def finish(self, *, generation: int) -> None: ...

    async def abort(self, *, generation: int) -> None: ...


class AnswerGenerationContinuation(Protocol):
    """現在の回答生成を継続できるか判定する。"""

    async def should_continue(self) -> bool: ...


class AnswerProgressReporter(Protocol):
    """agent core が回答工程の粗い進捗を通知する sink。"""

    async def stage_changed(self, stage: AnswerProgressStage) -> None: ...


class AnswerEventReporter(Protocol):
    """実装は best-effort とし、送信失敗を呼び出し元へ伝播させない。"""

    async def event_occurred(self, event: AnswerProgressEvent) -> None: ...


# 工程を跨いで共有される予算・上限の正本。ResearchHandoffがplanner input
# projectionとしてこのleafへ集約されたため、参照される側の定数も合わせてここへ
# 集約する。
RESEARCH_TASK_LIMIT = 3
MAX_ARTICLE_SEARCH_QUERIES = 3
RESEARCH_GOAL_MAX_CHARS: Final[int] = 200
# round-robin trimが各taskの先頭queryを必ず残せるのは、
# RESEARCH_TASK_LIMIT <= MAX_ARTICLE_SEARCH_QUERIESが前提。

EXTERNAL_TASK_QUERY_LIMIT = 3
EXTERNAL_QUERY_MAX_CHARS = 200
EVIDENCE_CLAIM_MAX_CHARS = 300
MISSING_ITEM_MAX_CHARS = 200

# Run 単位で reviewer が報告できる missing 件数の上限。
EVIDENCE_REVIEW_MISSING_LIMIT: Final[int] = 8

# 整理1本あたりの上限。thread全体を1本へ畳んだ結果であり、Run数によらず一定に保つ。
ORGANIZED_TEXT_MAX_CHARS: Final[int] = 600

_ExecutedQuery = Annotated[
    str,
    StringConstraints(min_length=1, max_length=EXTERNAL_QUERY_MAX_CHARS),
]
_OrganizedText = Annotated[
    str,
    StringConstraints(max_length=ORGANIZED_TEXT_MAX_CHARS),
]


class ResearchTaskRecord(BaseModel):
    """1 research taskの調査台帳。executed_queriesが空になるtaskは記録しない。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    research_goal: str = Field(min_length=1, max_length=RESEARCH_GOAL_MAX_CHARS)
    # provider呼び出しに成功した外部queryのみ。min 1件を型で強制する。
    executed_queries: tuple[_ExecutedQuery, ...] = Field(
        min_length=1,
        max_length=EXTERNAL_TASK_QUERY_LIMIT,
    )


class ResearchRunRecord(BaseModel):
    """1 Runが何を狙って何を叩いたかの台帳。

    plannerの「実行済みqueryを繰り返さない」規則がこの2つを文字列として読むため、
    LLMに畳ませず決定的に積む。何が得られたかはResearchHandoffの整理側が持つ。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: AwareDatetime
    # min 1件。0件になるRunは記録しない(builderがNoneを返す)。
    tasks: tuple[ResearchTaskRecord, ...] = Field(
        min_length=1,
        max_length=RESEARCH_TASK_LIMIT,
    )


class ResearchHandoff(BaseModel):
    """threadが積み上げた、次のRunへの調査の申し送り。

    台帳(runs)には上限を置かず、search Runごとに追記する。整理の3本は
    Runごとに積まず、research_handoff工程が毎回1本へ書き直す。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    updated_at: AwareDatetime
    # 古い順。1件目はhandoffを最初に書いたRun。
    runs: tuple[ResearchRunRecord, ...] = Field(min_length=1)
    collected_overview: _OrganizedText = ""
    unresolved_points: _OrganizedText = ""
    next_search_guidance: _OrganizedText = ""
