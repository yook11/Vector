"""分析済み記事の読み取り向けスキーマ。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from fastapi import Query
from pydantic import Field

if TYPE_CHECKING:
    from app.schemas.base import PaginationParams

from app.models.value_objects.category import CategorySlug
from app.schemas.base import PaginationParams, _CamelBase
from app.schemas.embeds import CategoryEmbed, NewsSourceEmbed, OriginalArticleEmbed

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


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


class ArticleBrief(_CamelBase):
    """GET /api/v1/articles — 一覧カード用

    per-user の watchlist 状態はこのスキーマには含めない。frontend は
    GET /api/v1/me/watchlist/ids を別途取得し render 時に Set lookup で
    merge する (Pattern B)。これにより /articles レスポンスは user 非依存
    となり HTTP cache/CDN 上で安全に共有できる。
    """

    id: int
    translated_title: str
    # 一覧カードの主表示。content のみ最大3件・各250字以内 (build_brief が保証)。
    # default 無し = required。空でも [] を必ず返し、欠落を契約違反にする。
    key_points: list[str]
    # key_points が空のときだけ summary を300字以内で返すフォールバック。
    # default 無し = required・nullable で、null でもキーを省略しない。
    summary_preview: str | None
    category: CategoryEmbed
    source: NewsSourceEmbed
    # 元記事の公開日時。分析工程に進む記事は必ず持つ (DB NOT NULL + ドメイン不変条件)。
    published_at: datetime


class ArticleDetail(_CamelBase):
    """GET /api/v1/articles/{id} — 詳細画面用"""

    id: int
    translated_title: str
    summary: str
    investor_take: str
    # 記事の重要な情報 (key_points[].content)。mentions は trends 内部利用のため
    # API 非公開。旧行 (NULL) や key_point 無し行では空配列になる。
    key_points: list[str] = Field(default_factory=list)
    analyzed_at: datetime
    category: CategoryEmbed
    source: NewsSourceEmbed
    published_at: datetime
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
