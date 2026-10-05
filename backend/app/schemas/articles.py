"""分析済み記事の読み取り向けスキーマ。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Path, Query
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.dataclasses import dataclass

from app.models.category import CATEGORY_SLUG_PATTERN
from app.schemas.base import _CamelBase
from app.schemas.category import Category
from app.schemas.cursor import CURSOR_JSON_SCHEMA, cursor_decoder
from app.schemas.embeds import NewsSourceEmbed, OriginalArticleEmbed

# 記事一覧・ウォッチリストが1回に返す件数。
ARTICLE_LIST_LIMIT = 24

# ---------------------------------------------------------------------------
# パスパラメータ
# ---------------------------------------------------------------------------


# 記事 ID 列は integer (int4) のため、範囲外の値は asyncpg の OverflowError より前に
# 422 で弾く (#545)。カーソルに入る記事 ID も同じ上限で検証する。
ARTICLE_ID_MAX = 2_147_483_647

ArticleId = Annotated[int, Path(ge=1, le=ARTICLE_ID_MAX)]


# ---------------------------------------------------------------------------
# クエリパラメータ
# ---------------------------------------------------------------------------


# category は外向き第一級フィルタキーで、形式は categories テーブルの制約と
# 同じ pattern で検証する。
# Topic は表示専用属性のため、フィルタキーとしては提供しない（2026-04 決定）。
_CATEGORY_QUERY_DESCRIPTION = "Outbound primary filter key. Accepts a category slug."


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class ArticleListPosition:
    """記事一覧の並び (公開日時の新しい順、同時刻は ID の大きい順) 上の位置。"""

    published_at: AwareDatetime
    id: Annotated[int, Field(ge=1, le=ARTICLE_ID_MAX)]


ArticleListCursor = Annotated[
    ArticleListPosition,
    BeforeValidator(cursor_decoder(ArticleListPosition)),
    CURSOR_JSON_SCHEMA,
]


class ArticleListParams(BaseModel):
    """記事一覧（ニュース閲覧）用のクエリパラメータ。

    category やカーソルの形式が不正なら 422 レスポンスを返す。
    ルーターでは Annotated[ArticleListParams, Query()] として受け取り、
    Service / Repository レイヤーへそのまま受け渡す。
    """

    category: Annotated[
        str | None,
        Query(pattern=CATEGORY_SLUG_PATTERN, description=_CATEGORY_QUERY_DESCRIPTION),
    ] = None
    cursor: Annotated[ArticleListCursor | None, Query()] = None


class AnalyzedArticlePreview(_CamelBase):
    """一覧用に分析済み記事を切り詰めた表現。要点の全件などは AnalyzedArticle が持つ。

    per-user の watchlist 状態はこのスキーマには含めない。frontend は
    GET /api/v1/me/watchlist/ids を別途取得し render 時に Set lookup で
    merge する (Pattern B)。これにより /articles レスポンスは user 非依存
    となり HTTP cache/CDN 上で安全に共有できる。
    """

    id: int
    translated_title: str
    # 一覧カードの主表示。content のみ最大3件・各250字以内
    # (build_analyzed_article_preview が保証)。
    # default 無し = required。空でも [] を必ず返し、欠落を契約違反にする。
    key_points: list[str]
    # key_points が空のときだけ summary を300字以内で返すフォールバック。
    # default 無し = required・nullable で、null でもキーを省略しない。
    summary_preview: str | None
    category: Category
    source: NewsSourceEmbed
    # 元記事の公開日時。分析工程に進む記事は必ず持つ (DB NOT NULL + ドメイン不変条件)。
    published_at: datetime


class AnalyzedArticle(_CamelBase):
    """分析済み記事について公開する情報をすべて持つ表現 (GET /api/v1/articles/{id})。"""

    id: int
    translated_title: str
    summary: str
    investor_take: str
    # 記事の重要な情報 (key_points[].content)。mentions は trends 内部利用のため
    # API 非公開。旧行 (NULL) や key_point 無し行では空配列になる。
    key_points: list[str] = Field(default_factory=list)
    analyzed_at: datetime
    category: Category
    source: NewsSourceEmbed
    published_at: datetime
    original: OriginalArticleEmbed


class AnalyzedArticlePreviewList(_CamelBase):
    """記事一覧・ウォッチリストの1回分と、続きの位置。"""

    items: list[AnalyzedArticlePreview]
    # 続きを取るときにそのまま cursor に渡す。null なら続きはない。
    # default 無し = required・nullable で、null でもキーを省略しない。
    next_cursor: str | None
