"""Internal search boundary型・error と Tool 契約。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle

__all__ = [
    "INTERNAL_SEARCH_TOOL_NAME",
    "InternalArticleContent",
    "InternalArticleSearchHit",
    "InternalSearchError",
    "InternalSearchFailurePhase",
    "InternalSearchTool",
    "InternalSearchToolInput",
    "InternalSearchToolName",
]

type InternalSearchFailurePhase = Literal["query_embedding", "article_search"]


class InternalSearchError(Exception):
    """回答継続可能と分類された内部検索の運用失敗。"""

    def __init__(self, *, phase: InternalSearchFailurePhase) -> None:
        super().__init__()
        self.phase = phase


class InternalArticleContent(BaseModel):
    """Answer-generation projection of an in-scope analyzed article."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    published_at: datetime | None = None

    @classmethod
    def from_article(
        cls,
        article: InScopeAnalyzedArticle,
        *,
        published_at: datetime | None,
    ) -> InternalArticleContent:
        mention_surfaces: list[str] = []
        seen_mentions: set[str] = set()
        for key_point in article.assessment_result.key_points:
            for mention in key_point.mentions:
                key = mention.surface.casefold()
                if key in seen_mentions:
                    continue
                seen_mentions.add(key)
                mention_surfaces.append(mention.surface)

        return cls(
            title=article.title,
            summary=article.summary,
            key_points=[
                key_point.content for key_point in article.assessment_result.key_points
            ],
            mentions=mention_surfaces,
            published_at=published_at,
        )


class InternalArticleSearchHit(BaseModel):
    """Internal vector search hit with the public /news article id."""

    model_config = ConfigDict(frozen=True)

    assessment_id: int = Field(gt=0)
    article: InScopeAnalyzedArticle
    content: InternalArticleContent
    distance: float = Field(ge=0)


InternalSearchToolName = Literal["internal_search"]
INTERNAL_SEARCH_TOOL_NAME: Final[InternalSearchToolName] = "internal_search"


@dataclass(frozen=True, slots=True)
class InternalSearchToolInput:
    """Internal Search Toolへ渡す正規化済みquery群。"""

    queries: InternalSearchQueries


class InternalSearchTool(Protocol):
    @property
    def name(self) -> InternalSearchToolName: ...

    async def search(
        self,
        input: InternalSearchToolInput,
    ) -> list[InternalArticleSearchHit]: ...
