"""Research response API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Query
from pydantic import AwareDatetime, Field, StringConstraints

from app.schemas.base import MAX_PER_PAGE, PaginationParams, _CamelBase
from app.shared.security.safe_url import SafeUrl

ResearchQuestion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]


class ResearchQuestionRequest(_CamelBase):
    question: ResearchQuestion
    thread_id: UUID | None = None


class ResearchInternalArticleSource(_CamelBase):
    kind: Literal["internal_article"]
    source_ref: str
    article_id: int | None
    title: str
    published_at: datetime | None


class ResearchExternalUrlSource(_CamelBase):
    kind: Literal["external_url"]
    source_ref: str
    url: SafeUrl
    title: str
    source_name: str | None
    published_at: datetime | None
    evidence_claim: str


ResearchSource = Annotated[
    ResearchInternalArticleSource | ResearchExternalUrlSource,
    Field(discriminator="kind"),
]


class ResearchRunStartResponse(_CamelBase):
    thread_id: UUID
    run_id: UUID


class ResearchDailyRequestLimitExceededResponse(_CamelBase):
    detail: Literal["Daily research request limit exceeded"]
    code: Literal["research_daily_request_limit_exceeded"]
    limit: Literal[10]
    reset_at: AwareDatetime


ResearchRunStatus = Literal["queued", "running", "completed", "failed"]
ResearchRunErrorCode = Literal[
    "generation_unavailable",
    "internal_error",
    "enqueue_failed",
    "stale",
    "cancelled",
]
ResearchProgressStage = Literal["planning", "retrieving", "synthesizing"]


class ResearchRunInternalSearchStartedEvent(_CamelBase):
    type: Literal["internal_search.started"]
    ts: datetime
    query_count: int = Field(ge=0)


class ResearchRunInternalSearchCompletedEvent(_CamelBase):
    type: Literal["internal_search.completed"]
    ts: datetime
    hit_count: int = Field(ge=0)


class ResearchRunExternalSearchQueriesGeneratedEvent(_CamelBase):
    type: Literal["external_search.queries_generated"]
    ts: datetime
    task_index: int = Field(ge=0)
    queries: list[str]


class ResearchRunExternalSearchCandidatesFetchedEvent(_CamelBase):
    type: Literal["external_search.candidates_fetched"]
    ts: datetime
    task_index: int = Field(ge=0)
    candidate_count: int = Field(ge=0)


class ResearchRunExternalSearchEvidenceSelectedEvent(_CamelBase):
    type: Literal["external_search.evidence_selected"]
    ts: datetime
    task_index: int = Field(ge=0)
    evidence_count: int = Field(ge=0)


class ResearchRunQuestionResolvedEvent(_CamelBase):
    type: Literal["question.resolved"]
    ts: datetime
    standalone_question: str = Field(min_length=1, max_length=500)


ResearchRunEvent = Annotated[
    ResearchRunInternalSearchStartedEvent
    | ResearchRunInternalSearchCompletedEvent
    | ResearchRunExternalSearchQueriesGeneratedEvent
    | ResearchRunExternalSearchCandidatesFetchedEvent
    | ResearchRunExternalSearchEvidenceSelectedEvent
    | ResearchRunQuestionResolvedEvent,
    Field(discriminator="type"),
]


class ResearchRunResponse(_CamelBase):
    run_id: UUID
    thread_id: UUID
    status: ResearchRunStatus
    error_code: ResearchRunErrorCode | None
    progress_stage: ResearchProgressStage | None
    attempt_epoch: int = Field(ge=0)
    recent_events: list[ResearchRunEvent] = Field(default_factory=list)


class ResearchThreadListParams(PaginationParams):
    per_page: Annotated[int, Query(ge=1, le=MAX_PER_PAGE, alias="perPage")] = 20


class ResearchThreadListItem(_CamelBase):
    thread_id: UUID
    title: str
    updated_at: datetime
    has_active_run: bool


class PaginatedResearchThreadResponse(_CamelBase):
    items: list[ResearchThreadListItem]
    total: int
    page: int
    per_page: int
    total_pages: int

    @classmethod
    def create(
        cls,
        *,
        items: list[ResearchThreadListItem],
        total: int,
        pagination: ResearchThreadListParams,
    ) -> PaginatedResearchThreadResponse:
        return cls(
            items=items,
            total=total,
            page=pagination.page,
            per_page=pagination.per_page,
            total_pages=pagination.total_pages(total),
        )


class ResearchMessageRun(_CamelBase):
    run_id: UUID
    status: ResearchRunStatus
    error_code: ResearchRunErrorCode | None
    progress_stage: ResearchProgressStage | None


class ResearchUserMessage(_CamelBase):
    role: Literal["user"]
    seq: int
    content: str
    created_at: datetime
    run: ResearchMessageRun


class ResearchAssistantMessage(_CamelBase):
    role: Literal["assistant"]
    seq: int
    content: str = Field(
        description=(
            "Generated answer text. Evidence-grounded answers may include inline "
            "citation markers like [[1]], where the number matches sources[].sourceRef."
        )
    )
    created_at: datetime
    sources: list[ResearchSource]
    missing_aspects: list[str]


ResearchThreadMessage = Annotated[
    ResearchUserMessage | ResearchAssistantMessage,
    Field(discriminator="role"),
]


class ResearchThreadDetail(_CamelBase):
    thread_id: UUID
    title: str
    messages: list[ResearchThreadMessage]
