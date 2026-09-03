"""Internal search の境界型・error と port 契約。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle

__all__ = [
    "INTERNAL_SEARCH_HIT_POOL_LIMIT",
    "INTERNAL_SEARCH_HITS_PER_QUERY",
    "InternalArticleContent",
    "InternalArticleSearchHit",
    "InternalSearch",
    "InternalSearchError",
    "InternalSearchFailureCode",
    "InternalSearchOutcome",
]

type InternalSearchOutcome = Literal["succeeded", "empty", "failed"]

INTERNAL_SEARCH_HITS_PER_QUERY = 5
INTERNAL_SEARCH_HIT_POOL_LIMIT = 5


class InternalSearchFailureCode(StrEnum):
    """内部検索が安全に分類できる失敗理由。"""

    TIMEOUT = "internal_search_timeout"
    QUERY_EMBEDDING_FAILED = "query_embedding_failed"
    ARTICLE_SEARCH_FAILED = "article_search_failed"


class InternalSearchError(Exception):
    """回答継続可能と分類された内部検索の運用失敗。"""

    def __init__(self, *, code: InternalSearchFailureCode) -> None:
        if not isinstance(code, InternalSearchFailureCode):
            raise TypeError("code must be an InternalSearchFailureCode")
        self.code = code
        super().__init__(code.value)


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


class InternalSearch(Protocol):
    async def search(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalArticleSearchHit]: ...
