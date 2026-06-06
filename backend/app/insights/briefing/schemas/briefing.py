"""GET /api/v1/briefing/{categorySlug} のレスポンス schema (camelCase)。

設計判断:
- カテゴリは存在する slug のみ受け付け、未生成は ``state="empty"`` で 200 を返す
  (snapshot router と同パターン、failure_visibility)
- 不明な category slug は 404 (resource として存在しないため)
- ``keyArticles[].articleId`` から参照される記事詳細を ``articles`` lookup table と
  して 1 リクエストでまとめて返す (frontend で N+1 fetch しないで済む)

サイズ上限 (red-team F10 構造防御):
    各 str / list の max_length は anon GET 経路で巨大 JSONB が response として
    流れる経路を FastAPI ``response_model`` serialize 時に reject する
    (``ResponseValidationError`` → 500、failure_visibility 方針)。
    domain 側の ``WeeklyBriefingContent`` と同値で持ち、二箇所で同じ振る舞いを保証する。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Final, Literal

from pydantic import Field

from app.insights.briefing.domain.briefing import (
    MAX_BRIEFING_HEADLINE_LEN,
    MAX_BRIEFING_OVERVIEW_LEN,
    MAX_KEY_ARTICLE_SIGNIFICANCE_LEN,
    MAX_KEY_ARTICLES_PER_BRIEFING,
    MAX_WATCH_POINT_STATEMENT_LEN,
    MAX_WATCH_POINTS_PER_BRIEFING,
)
from app.models.value_objects.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase

# response 固有の上限 (domain VO に対応物がない参照記事系)。key_articles は
# 記事 id を 1 件ずつ持つため、参照記事数の上限は key_articles 件数上限と一致する。
_MAX_REFERENCED_ARTICLES: Final[int] = MAX_KEY_ARTICLES_PER_BRIEFING
# 記事サマリ 1 件分の表示用文字列上限。NewsSource.name / 翻訳タイトル / URL が対象。
_MAX_ARTICLE_TITLE_LEN: Final[int] = 500
_MAX_SOURCE_NAME_LEN: Final[int] = 200
_MAX_URL_LEN: Final[int] = 2_000
# カテゴリ数 (現在 11、将来余裕で 20)。
_MAX_BRIEFING_LIST_ITEMS: Final[int] = 20


class _KeyArticleOut(_CamelBase):
    article_id: int
    significance: str = Field(max_length=MAX_KEY_ARTICLE_SIGNIFICANCE_LEN)


class _WatchPointOut(_CamelBase):
    statement: str = Field(max_length=MAX_WATCH_POINT_STATEMENT_LEN)


class _ArticleSummaryOut(_CamelBase):
    """``keyArticles[].articleId`` から参照される記事のサマリ。"""

    id: int
    title_ja: str = Field(max_length=_MAX_ARTICLE_TITLE_LEN)
    source_name: str = Field(max_length=_MAX_SOURCE_NAME_LEN)
    url: str = Field(max_length=_MAX_URL_LEN)


class _CategoryOut(_CamelBase):
    id: int
    slug: CategorySlug
    name: CategoryName


class ReadyBriefing(_CamelBase):
    """briefing 生成済の状態。"""

    state: Literal["ready"] = "ready"
    week_start: date
    generated_at: datetime
    model_name: str
    input_article_count: int
    category: _CategoryOut
    headline: str = Field(max_length=MAX_BRIEFING_HEADLINE_LEN)
    overview: str = Field(max_length=MAX_BRIEFING_OVERVIEW_LEN)
    key_articles: list[_KeyArticleOut] = Field(max_length=MAX_KEY_ARTICLES_PER_BRIEFING)
    watch_points: list[_WatchPointOut] = Field(max_length=MAX_WATCH_POINTS_PER_BRIEFING)
    articles: list[_ArticleSummaryOut] = Field(max_length=_MAX_REFERENCED_ARTICLES)


class EmptyBriefing(_CamelBase):
    """指定カテゴリに briefing 未生成の状態。"""

    state: Literal["empty"] = "empty"
    category: _CategoryOut


BriefingResponse = Annotated[
    ReadyBriefing | EmptyBriefing,
    Field(discriminator="state"),
]


class _BriefingListLatest(_CamelBase):
    """一覧行に同梱する「最新 briefing 参照」。

    未生成カテゴリでは ``BriefingListItem.latest = None`` で表現する。
    詳細 (``ReadyBriefing``) と異なり overview / keyArticles 等は持たず、
    一覧で表示する短い見出しのみ。
    """

    week_start: date
    headline: str = Field(max_length=MAX_BRIEFING_HEADLINE_LEN)


class BriefingListItem(_CamelBase):
    """一覧 1 行: カテゴリ + 最新 briefing 参照 (未生成は None)。"""

    category: _CategoryOut
    latest: _BriefingListLatest | None


class BriefingListResponse(_CamelBase):
    """``GET /api/v1/briefing`` のレスポンス。

    ``items`` は ``Category.id`` 昇順で 11 カテゴリ全部を返す。並び順は
    backend で確定し、frontend での sort を不要にする。
    """

    current_week_start: date
    items: list[BriefingListItem] = Field(max_length=_MAX_BRIEFING_LIST_ITEMS)
