"""Research response API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from app.agent.contract import (
    AnswerQuestionResult,
    ExternalUrlSource,
    InternalArticleSource,
)
from app.schemas.base import _CamelBase
from app.shared.security.safe_url import SafeUrl

ResearchQuestion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]


class ResearchQuestionRequest(_CamelBase):
    question: ResearchQuestion


class ResearchInternalArticleSource(_CamelBase):
    kind: Literal["internal_article"]
    source_ref: str
    article_id: int
    title: str
    source_name: str | None
    published_at: datetime | None
    snippet: str | None

    @classmethod
    def from_source(
        cls, source: InternalArticleSource
    ) -> ResearchInternalArticleSource:
        return cls(
            kind=source.kind,
            source_ref=source.source_ref,
            article_id=source.article_id,
            title=source.title,
            source_name=source.source_name,
            published_at=source.published_at,
            snippet=source.snippet,
        )


class ResearchExternalUrlSource(_CamelBase):
    kind: Literal["external_url"]
    source_ref: str
    url: SafeUrl
    title: str
    source_name: str | None
    published_at: datetime | None
    snippet: str | None

    @classmethod
    def from_source(cls, source: ExternalUrlSource) -> ResearchExternalUrlSource:
        return cls(
            kind=source.kind,
            source_ref=source.source_ref,
            url=source.url,
            title=source.title,
            source_name=source.source_name,
            published_at=source.published_at,
            snippet=source.snippet,
        )


ResearchSource = Annotated[
    ResearchInternalArticleSource | ResearchExternalUrlSource,
    Field(discriminator="kind"),
]


class ResearchResponse(_CamelBase):
    answer: str
    sources: list[ResearchSource]
    missing_aspects: list[str]

    @classmethod
    def from_result(cls, result: AnswerQuestionResult) -> ResearchResponse:
        sources: list[ResearchSource] = []
        for source in result.sources:
            match source:
                case InternalArticleSource():
                    sources.append(ResearchInternalArticleSource.from_source(source))
                case ExternalUrlSource():
                    sources.append(ResearchExternalUrlSource.from_source(source))
        return cls(
            answer=result.answer,
            sources=sources,
            missing_aspects=result.missing_aspects,
        )
