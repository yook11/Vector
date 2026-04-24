"""分析済み記事の読み取り向けスキーマ。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from fastapi import Query
from pydantic import field_validator

if TYPE_CHECKING:
    from app.schemas.base import PaginationParams

from app.analysis.domain.value_objects.impact_level import ImpactLevel
from app.domain.category import CategorySlug
from app.schemas.base import PaginationParams, _CamelBase
from app.schemas.embeds import NewsSourceEmbed, OriginalArticleEmbed, TopicEmbed

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
# クエリパラメータ — VO 型を全レイヤーに通す
# ---------------------------------------------------------------------------


class ArticleListParams(PaginationParams):
    """記事一覧（ニュース閲覧）用のクエリパラメータ。

    page/per_page は PaginationParams から継承する。
    VO フィールド（CategorySlug）はクエリパラメータのパース時に
    Pydantic が直接検証し、不正値は 422 レスポンスを返す。
    ルーターでは Annotated[ArticleListParams, Query()] として受け取り、
    Service / Repository レイヤーへそのまま受け渡す。
    """

    topic: Annotated[str | None, Query()] = None
    category: Annotated[CategorySlug | None, Query()] = None
    impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC


class SemanticSearchParams(PaginationParams):
    """セマンティック検索（分析探索）用のクエリパラメータ。

    一覧と検索は本質的に別の操作なので ArticleListParams とは分離する。
    現状のフィルタ項目は重複しているが、検索側は投資分析固有の
    フィルタが増えるにつれて分岐していく想定。
    """

    q: Annotated[str, Query(min_length=1, max_length=500)]
    sort_by: Annotated[SortBy, Query(alias="sortBy")] = SortBy.RELEVANCE
    topic: Annotated[str | None, Query()] = None
    category: Annotated[CategorySlug | None, Query()] = None
    impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC

    @field_validator("q", mode="after")
    @classmethod
    def _normalize_q(cls, v: str) -> str:
        """キャッシュキーの些細な差異を吸収するため検索クエリを正規化する。"""
        return " ".join(v.lower().split())


class ArticleBrief(_CamelBase):
    """GET /api/v1/articles — 一覧カード用"""

    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    source: NewsSourceEmbed
    published_at: datetime | None = None
    topic: TopicEmbed | None = None
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
    topic: TopicEmbed | None = None
    is_watched: bool = False
    original: OriginalArticleEmbed


class PaginatedArticleResponse(_CamelBase):
    """記事のページネーション付きリスト。"""

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
