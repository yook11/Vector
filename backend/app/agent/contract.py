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
    "ANSWER_EVIDENCE_LIMIT",
    "EVIDENCE_CLAIM_MAX_CHARS",
    "EVIDENCE_REVIEW_MISSING_LIMIT",
    "EVIDENCE_REVIEWER_SELECTION_LIMIT",
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
    "PRIOR_RESEARCH_CHECKPOINT_LIMIT",
    "QuestionResolvedEvent",
    "PlanType",
    "RESEARCH_GOAL_MAX_CHARS",
    "RESEARCH_TASK_LIMIT",
    "ResearchCheckpoint",
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


class QuestionResolvedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["context_resolution.question_resolved"] = (
        "context_resolution.question_resolved"
    )
    standalone_question: str = Field(min_length=1, max_length=500)


AnswerProgressEvent = Annotated[
    InternalSearchStartedEvent
    | InternalSearchCompletedEvent
    | ExternalSearchQueriesGeneratedEvent
    | ExternalSearchHitsFetchedEvent
    | EvidenceReviewSelectedEvent
    | QuestionResolvedEvent,
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


# 工程を跨いで共有される予算・上限の正本。ResearchCheckpointがplanner input
# projectionとしてこのleafへ集約されたため、参照される側の定数も合わせてここへ
# 集約する(各工程のcontract.pyはこれをre-exportし、参照元は無変更)。
RESEARCH_TASK_LIMIT = 3
MAX_ARTICLE_SEARCH_QUERIES = 3
RESEARCH_GOAL_MAX_CHARS: Final[int] = 200
# round-robin trimが各taskの先頭queryを必ず残せるのは、
# RESEARCH_TASK_LIMIT <= MAX_ARTICLE_SEARCH_QUERIESが前提。

EXTERNAL_TASK_QUERY_LIMIT = 3
EXTERNAL_QUERY_MAX_CHARS = 200
EVIDENCE_CLAIM_MAX_CHARS = 300
MISSING_ITEM_MAX_CHARS = 200
# Reviewerへ見せるsnippetの最大長。外部hitはprovider応答時、内部hitは投影時に切る。
OPTION_SNIPPET_MAX_CHARS = 500

# Run 単位で Reviewer が返せる選択件数と、Answerer に渡せる一意な根拠件数の上限。
EVIDENCE_REVIEWER_SELECTION_LIMIT: Final[int] = 15
ANSWER_EVIDENCE_LIMIT: Final[int] = 15
# Run 単位で reviewer が報告できる missing 件数の上限。
EVIDENCE_REVIEW_MISSING_LIMIT: Final[int] = 8

# 後続Runへ注入する直近checkpoint件数の正本(注入フロー2)。
PRIOR_RESEARCH_CHECKPOINT_LIMIT: Final[int] = 3

_ExecutedQuery = Annotated[
    str,
    StringConstraints(min_length=1, max_length=EXTERNAL_QUERY_MAX_CHARS),
]
_AdoptedClaim = Annotated[str, StringConstraints(max_length=EVIDENCE_CLAIM_MAX_CHARS)]
_UnresolvedItem = Annotated[str, StringConstraints(max_length=MISSING_ITEM_MAX_CHARS)]


class ResearchTaskRecord(BaseModel):
    """1 research taskの調査記録。executed_queriesが空になるtaskは記録しない。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    research_goal: str = Field(min_length=1, max_length=RESEARCH_GOAL_MAX_CHARS)
    # provider呼び出しに成功した外部queryのみ。min 1件を型で強制する。
    executed_queries: tuple[_ExecutedQuery, ...] = Field(
        min_length=1,
        max_length=EXTERNAL_TASK_QUERY_LIMIT,
    )
    # 外部検索から採用されたclaim。空 = 有用な選択肢なし。
    # Run全体(Checkpoint全task合計)の上限はResearchCheckpointのvalidatorが持つ。
    adopted_claims: tuple[_AdoptedClaim, ...]


class ResearchCheckpoint(BaseModel):
    """Runが実行した外部検索の決定的な記録。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    as_of: AwareDatetime
    # min 1件。0件になるRunはcolumnをNULLにする(builderがNoneを返す)。
    tasks: tuple[ResearchTaskRecord, ...] = Field(
        min_length=1,
        max_length=RESEARCH_TASK_LIMIT,
    )
    # Evidence Reviewerのmissingのverbatim copy。Run全体で1本。
    unresolved_after_search: tuple[_UnresolvedItem, ...] = Field(
        max_length=EVIDENCE_REVIEW_MISSING_LIMIT,
    )

    @model_validator(mode="after")
    def _validate_total_adopted_claims(self) -> Self:
        # adopted_claimsの上限はtask個別ではなくCheckpoint全task合計。
        total_adopted_claims = sum(len(task.adopted_claims) for task in self.tasks)
        if total_adopted_claims > ANSWER_EVIDENCE_LIMIT:
            raise ValueError(
                "adopted claims across tasks exceed the answer evidence limit"
            )
        return self
