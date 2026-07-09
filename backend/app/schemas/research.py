"""Research response API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from app.schemas.base import _CamelBase
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


class ResearchResponse(_CamelBase):
    answer: str = Field(
        description=(
            "Generated answer text. Evidence-grounded answers may include inline "
            "citation markers like [[1]], where the number matches sources[].sourceRef."
        )
    )
    sources: list[ResearchSource]
    missing_aspects: list[str]


class ResearchRunStartResponse(_CamelBase):
    thread_id: UUID
    run_id: UUID


class ResearchRunResponse(_CamelBase):
    run_id: UUID
    thread_id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    result: ResearchResponse | None
    error_code: (
        Literal[
            "generation_unavailable",
            "internal_error",
            "enqueue_failed",
            "stale",
        ]
        | None
    )
