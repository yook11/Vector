"""分析済み記事の読み取り向けスキーマ。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from fastapi import Query
from pydantic import field_validator

if TYPE_CHECKING:
    from app.schemas.base import PaginationParams

from app.domain.category import CategorySlug
from app.schemas.base import PaginationParams, _CamelBase
from app.schemas.embeds import NewsSourceEmbed, OriginalArticleEmbed

SEARCH_QUERY_MAX_LENGTH = 200

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


# category は外向き第一級フィルタキー。サイドバーから渡される CategorySlug を受け取り
# Pydantic がパース時に正規化・検証する。
# Topic は表示専用属性のため、フィルタキーとしては提供しない（2026-04 決定）。
_CATEGORY_QUERY_DESCRIPTION = "Outbound primary filter key. Accepts a category slug."


class ArticleListParams(PaginationParams):
    """記事一覧（ニュース閲覧）用のクエリパラメータ。

    page/per_page は PaginationParams から継承する。
    VO フィールド（CategorySlug）はクエリパラメータのパース時に
    Pydantic が直接検証し、不正値は 422 レスポンスを返す。
    ルーターでは Annotated[ArticleListParams, Query()] として受け取り、
    Service / Repository レイヤーへそのまま受け渡す。
    """

    category: Annotated[
        CategorySlug | None,
        Query(description=_CATEGORY_QUERY_DESCRIPTION),
    ] = None
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC


class SemanticSearchParams(PaginationParams):
    """セマンティック検索（分析探索）用のクエリパラメータ。

    一覧と検索は本質的に別の操作なので ArticleListParams とは分離する。
    現状のフィルタ項目は重複しているが、検索側は投資分析固有の
    フィルタが増えるにつれて分岐していく想定。
    """

    q: Annotated[str, Query(min_length=1, max_length=SEARCH_QUERY_MAX_LENGTH)]
    sort_by: Annotated[SortBy, Query(alias="sortBy")] = SortBy.RELEVANCE
    category: Annotated[
        CategorySlug | None,
        Query(description=_CATEGORY_QUERY_DESCRIPTION),
    ] = None
    sort_order: Annotated[SortOrder, Query(alias="sortOrder")] = SortOrder.DESC

    @field_validator("q", mode="after")
    @classmethod
    def _normalize_q(cls, v: str) -> str:
        """キャッシュキーの些細な差異を吸収するため検索クエリを正規化する。"""
        q = " ".join(v.lower().split())
        if not q:
            msg = "Search query must not be blank"
            raise ValueError(msg)
        return q


class ArticleBrief(_CamelBase):
    """GET /api/v1/articles — 一覧カード用

    per-user の watchlist 状態はこのスキーマには含めない。frontend は
    GET /api/v1/me/watchlist/ids を別途取得し render 時に Set lookup で
    merge する (Pattern B)。これにより /articles レスポンスは user 非依存
    となり HTTP cache/CDN 上で安全に共有できる。
    """

    id: int
    translated_title: str
    summary: str
    source: NewsSourceEmbed
    published_at: datetime | None = None


class ArticleDetail(_CamelBase):
    """GET /api/v1/articles/{id} — 詳細画面用"""

    id: int
    translated_title: str
    summary: str
    investor_take: str
    analyzed_at: datetime
    source: NewsSourceEmbed
    published_at: datetime | None = None
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
