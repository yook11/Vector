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
