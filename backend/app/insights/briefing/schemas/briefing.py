"""GET /api/v1/briefing/{categorySlug} のレスポンス schema (camelCase)。

設計判断:
- カテゴリは存在する slug のみ受け付け、未生成は ``state="empty"`` で 200 を返す
  (snapshot router と同パターン、failure_visibility)
- 不明な category slug は 404 (resource として存在しないため)
- ``stories[].articleIds`` から参照される記事詳細を ``articles`` lookup table と
  して 1 リクエストでまとめて返す (frontend で N+1 fetch しないで済む)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field

from app.domain.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase


class _StoryOut(_CamelBase):
    title: str
    analysis: str
    article_ids: list[int]


class _ArticleSummaryOut(_CamelBase):
    """``stories[].articleIds`` から参照される記事のサマリ。"""

    id: int
    title_ja: str
    source_name: str
    url: str


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
    headline: str
    stories: list[_StoryOut]
    articles: list[_ArticleSummaryOut]


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
    詳細 (``ReadyBriefing``) と異なり stories 等は持たず、newspaper 風
    プレビューに必要な最小フィールドのみ。
    """

    week_start: date
    headline_excerpt: str


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
    items: list[BriefingListItem]
