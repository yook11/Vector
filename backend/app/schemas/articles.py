"""Read-facing schemas for analyzed articles."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from fastapi import Query
from pydantic import field_validator

if TYPE_CHECKING:
    from app.schemas.base import PaginationParams

from app.domain.category import CategorySlug
from app.domain.keyword import KeywordName
from app.domain.news_source import SourceName
from app.models.article_analysis import ImpactLevel
from app.schemas.base import PaginationParams, _CamelBase
from app.schemas.embeds import KeywordEmbed, NewsSourceEmbed, OriginalArticleEmbed

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SortBy(StrEnum):
    DATE = "date"
    RELEVANCE = "relevance"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


# ---------------------------------------------------------------------------
# Query parameters — VO types flow through all layers
# ---------------------------------------------------------------------------


class ArticleListParams(PaginationParams):
    """Query parameters for article listing (news browsing).

    Inherits page/per_page from PaginationParams.
    VO fields (CategorySlug, KeywordName, SourceName) are validated directly by
    Pydantic during query parameter parsing — invalid values produce a 422
    response. Received in the router via Annotated[ArticleListParams, Query()]
    and passed through to Service and Repository layers unchanged.
    """

    keyword: Annotated[KeywordName | None, Query()] = None
    category: Annotated[CategorySlug | None, Query()] = None
    source: Annotated[SourceName | None, Query()] = None
    impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC


class SemanticSearchParams(PaginationParams):
    """Query parameters for semantic search (analytical exploration).

    Separate from ArticleListParams because listing and search are
    fundamentally different operations. Filter fields overlap today but
    will diverge as search gains investment-analysis-specific filters.
    """

    q: Annotated[str, Query(min_length=1, max_length=500)]
    sort_by: Annotated[SortBy, Query(alias="sortBy")] = SortBy.RELEVANCE
    keyword: Annotated[KeywordName | None, Query()] = None
    category: Annotated[CategorySlug | None, Query()] = None
    source: Annotated[SourceName | None, Query()] = None
    impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC

    @field_validator("q", mode="after")
    @classmethod
    def _normalize_q(cls, v: str) -> str:
        """Normalize the search query so cache keys collapse trivial variants."""
        return " ".join(v.lower().split())


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

    @classmethod
    def create(
        cls,
        items: list[ArticleBrief],
        total: int,
        pagination: PaginationParams,
    ) -> PaginatedArticleResponse:
        return cls(
            items=items,
            total=total,
            page=pagination.page,
            per_page=pagination.per_page,
            total_pages=pagination.total_pages(total),
        )
