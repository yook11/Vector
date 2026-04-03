"""Read-facing schemas for analyzed articles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

from app.domain.category import CategorySlug
from app.domain.news_source import SourceName
from app.models.article_analysis import ImpactLevel
from app.schemas.base import _CamelBase
from app.schemas.embeds import KeywordEmbed, NewsSourceEmbed, OriginalArticleEmbed

# ---------------------------------------------------------------------------
# Enums for article listing
# ---------------------------------------------------------------------------


class ArticleSortField(StrEnum):
    PUBLISHED_AT = "publishedAt"
    IMPACT_LEVEL = "impactLevel"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


# ---------------------------------------------------------------------------
# Request params (raw values) — Router layer via Depends()
# ---------------------------------------------------------------------------


class ArticleListParams(BaseModel):
    """Raw request parameters for article listing.

    Pure parameter definition — no VO conversion, no error handling.
    Used via Depends() in the Router layer.
    """

    keyword_id: Annotated[int | None, Query(alias="keywordId")] = None
    category: Annotated[str | None, Query()] = None
    source: Annotated[str | None, Query()] = None
    impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None
    q: Annotated[str | None, Query(min_length=1, max_length=500)] = None
    sort_by: Annotated[ArticleSortField, Query(alias="sortBy")] = (
        ArticleSortField.PUBLISHED_AT
    )
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC
    page: Annotated[int, Query(ge=1)] = 1
    per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 12


# ---------------------------------------------------------------------------
# Resolved query (VO types) — Service / Repository layers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArticleListQuery:
    """Resolved query with validated VO types.

    Consumed by Service and Repository layers.
    """

    keyword_id: int | None = None
    category_slug: CategorySlug | None = None
    source_name: SourceName | None = None
    impact_level: ImpactLevel | None = None
    q: str | None = None
    sort_by: ArticleSortField = ArticleSortField.PUBLISHED_AT
    sort_order: SortOrder = SortOrder.DESC
    page: int = 1
    per_page: int = 12


class ArticleBrief(_CamelBase):
    """GET /api/v1/articles — 一覧カード用"""

    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    source: NewsSourceEmbed
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False


class ArticleDetail(_CamelBase):
    """GET /api/v1/articles/{id} — 詳細画面用"""

    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    analyzed_at: datetime
    source: NewsSourceEmbed
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False
    original: OriginalArticleEmbed


class PaginatedArticleResponse(_CamelBase):
    """Paginated list of articles."""

    items: list[ArticleBrief]
    total: int
    page: int
    per_page: int
    total_pages: int
