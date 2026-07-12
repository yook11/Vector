"""Agent core の最小入出力 contract。

API / UI / graph runtime から独立した final result の型だけをここで保証する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
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
    "AnswerQuestionInput",
    "AnswerProgressReporter",
    "AnswerProgressEvent",
    "AnswerProgressStage",
    "AnswerQuestionResult",
    "AnswerRetrievalSummary",
    "AnswerEventReporter",
    "ExternalSearchCandidatesFetchedEvent",
    "ExternalSearchEvidenceSelectedEvent",
    "ExternalSearchQueriesGeneratedEvent",
    "AnswerSource",
    "ExternalUrlSource",
    "InternalSearchCompletedEvent",
    "InternalSearchStartedEvent",
    "InternalArticleSource",
    "NonBlankText",
    "QuestionAnsweringAgent",
    "QuestionResolvedEvent",
    "RetrievalMode",
    "EvidenceCollectionFailure",
]

RetrievalMode = Literal["none", "internal", "external", "internal_and_external"]
EvidenceCollectionFailure = Literal["internal_search", "external_search"]
AnswerProgressStage = Literal["planning", "retrieving", "synthesizing"]
NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AnswerQuestionInput(BaseModel):
    """ユーザー質問と実行基準時刻を agent core に渡す入力。"""

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1)
    as_of: datetime
    user_intent: str = ""
    prior_coverage: str = ""
    user_activity_context: str = ""
    previous_answer: str = ""


class AnswerRetrievalSummary(BaseModel):
    """planner が必要と判断した情報取得と、失敗した収集経路。"""

    model_config = ConfigDict(frozen=True)

    planned_mode: RetrievalMode
    collection_failures: list[EvidenceCollectionFailure] = Field(default_factory=list)


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

    type: Literal["internal_search.started"] = "internal_search.started"
    query_count: int = Field(ge=0)


class InternalSearchCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["internal_search.completed"] = "internal_search.completed"
    hit_count: int = Field(ge=0)


class ExternalSearchQueriesGeneratedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["external_search.queries_generated"] = (
        "external_search.queries_generated"
    )
    task_index: int = Field(ge=0)
    queries: list[NonBlankText] = Field(default_factory=list)


class ExternalSearchCandidatesFetchedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["external_search.candidates_fetched"] = (
        "external_search.candidates_fetched"
    )
    task_index: int = Field(ge=0)
    candidate_count: int = Field(ge=0)


class ExternalSearchEvidenceSelectedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["external_search.evidence_selected"] = (
        "external_search.evidence_selected"
    )
    task_index: int = Field(ge=0)
    evidence_count: int = Field(ge=0)


class QuestionResolvedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["question.resolved"] = "question.resolved"
    standalone_question: str = Field(min_length=1, max_length=500)


AnswerProgressEvent = Annotated[
    InternalSearchStartedEvent
    | InternalSearchCompletedEvent
    | ExternalSearchQueriesGeneratedEvent
    | ExternalSearchCandidatesFetchedEvent
    | ExternalSearchEvidenceSelectedEvent
    | QuestionResolvedEvent,
    Field(discriminator="type"),
]


class AnswerQuestionResult(BaseModel):
    """chat UI に変換される agent core の final result。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["answered", "insufficient"]
    answer: NonBlankText
    sources: list[AnswerSource] = Field(default_factory=list)
    missing_aspects: list[NonBlankText] = Field(default_factory=list)
    retrieval: AnswerRetrievalSummary

    @model_validator(mode="after")
    def _validate_provenance(self) -> Self:
        if self.status == "answered":
            if self.retrieval.planned_mode != "none" and not self.sources:
                raise ValueError("non-direct answered result must include a source")
            if self.missing_aspects:
                raise ValueError("answered result cannot include missing aspects")
            if self.retrieval.collection_failures:
                raise ValueError("answered result cannot include collection failures")
        if self.status == "insufficient" and not self.missing_aspects:
            raise ValueError("insufficient result must include missing aspects")
        if self.retrieval.planned_mode == "none" and self.sources:
            raise ValueError("direct planned result cannot include sources")
        return self


class QuestionAnsweringAgent(Protocol):
    """agent core の最小呼び出し口。"""

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult: ...


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
