"""GET /api/v1/briefing/{categorySlug} のレスポンス schema (camelCase)。

設計判断:
- カテゴリは存在する slug のみ受け付け、未生成は ``state="empty"`` で 200 を返す
  (trends router と同パターン、failure_visibility)
- 不明な category slug は 404 (resource として存在しないため)
- ``keyArticles[]`` は編集判断 (significance) と参照記事 (``article``) を
  自己完結 nested で返す (frontend に lookup join を強いない)

サイズ上限 (red-team F10 構造防御):
    各 str / list の max_length は ``require_bff_request`` 保護下の共有 read で
    巨大 JSONB が response として流れる経路を FastAPI ``response_model``
    serialize 時に reject する (``ResponseValidationError`` → 500、
    failure_visibility 方針)。
    domain 側の ``WeeklyBriefingContent`` と同値で持ち、二箇所で同じ振る舞いを保証する。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Final, Literal

from pydantic import Field

from app.analysis.assessment.domain.result import (
    MAX_KEY_POINT_CONTENT_LEN,
    MAX_KEY_POINTS_PER_ASSESSMENT,
)
from app.insights.briefing.domain.briefing import (
    MAX_BRIEFING_HEADLINE_LEN,
    MAX_BRIEFING_SUMMARY_LEN,
    MAX_CHAPTER_BODY_LEN,
    MAX_CHAPTER_HEADING_LEN,
    MAX_CHAPTERS_PER_BRIEFING,
    MAX_KEY_ARTICLE_SIGNIFICANCE_LEN,
    MAX_KEY_ARTICLES_PER_BRIEFING,
    MAX_WATCH_POINT_STATEMENT_LEN,
    MAX_WATCH_POINTS_PER_BRIEFING,
)
from app.schemas.base import _CamelBase
from app.schemas.embeds import CategoryEmbed, NewsSourceEmbed

# 記事 embed 1 件分の表示用文字列上限。翻訳タイトル / URL が対象。
_MAX_ARTICLE_TITLE_LEN: Final[int] = 500
_MAX_URL_LEN: Final[int] = 2_000
# カテゴリ数 (現在 11、将来余裕で 20)。
_MAX_BRIEFING_LIST_ITEMS: Final[int] = 20


class _BriefingChapter(_CamelBase):
    heading: str = Field(max_length=MAX_CHAPTER_HEADING_LEN)
    body: str = Field(max_length=MAX_CHAPTER_BODY_LEN)


class _BriefingArticleEmbed(_CamelBase):
    """``keyArticles[].article`` に埋め込む参照記事 (読み出し時 join)。

    記事側の現在の事実を運ぶ。``id`` は ``/news/{id}`` 記事詳細の公開 id
    (``ArticleBrief.id`` と同じ id 空間)。
    """

    id: int
    translated_title: str = Field(max_length=_MAX_ARTICLE_TITLE_LEN)
    source: NewsSourceEmbed
    url: str = Field(max_length=_MAX_URL_LEN)
    # 元記事の公開日時 (AnalyzableArticleRecord.published_at)。DB NOT NULL。
    published_at: datetime
    # 上限は assessment 側入口契約 (domain/result.py) の定数を共有 (F10 ガード)。
    key_points: list[Annotated[str, Field(max_length=MAX_KEY_POINT_CONTENT_LEN)]] = (
        Field(max_length=MAX_KEY_POINTS_PER_ASSESSMENT)
    )


class _BriefingKeyArticle(_CamelBase):
    """briefing の編集判断 (生成時固定) + 参照記事 (読み出し時 join) の自己完結ペア。"""

    significance: str = Field(max_length=MAX_KEY_ARTICLE_SIGNIFICANCE_LEN)
    article: _BriefingArticleEmbed


class BriefingDetail(_CamelBase):
    """briefing 生成済の状態。"""

    state: Literal["briefing"] = "briefing"
    week_start: date
    generated_at: datetime
    model_name: str
    input_article_count: int
    category: CategoryEmbed
    headline: str = Field(max_length=MAX_BRIEFING_HEADLINE_LEN)
    summary: str = Field(max_length=MAX_BRIEFING_SUMMARY_LEN)
    chapters: list[_BriefingChapter] = Field(max_length=MAX_CHAPTERS_PER_BRIEFING)
    key_articles: list[_BriefingKeyArticle] = Field(
        max_length=MAX_KEY_ARTICLES_PER_BRIEFING
    )
    watch_points: list[
        Annotated[str, Field(max_length=MAX_WATCH_POINT_STATEMENT_LEN)]
    ] = Field(max_length=MAX_WATCH_POINTS_PER_BRIEFING)


class EmptyBriefing(_CamelBase):
    """指定カテゴリに briefing 未生成の状態。"""

    state: Literal["empty"] = "empty"
    category: CategoryEmbed


BriefingResponse = Annotated[
    BriefingDetail | EmptyBriefing,
    Field(discriminator="state"),
]


class BriefingSummary(_CamelBase):
    """一覧行に同梱する briefing 要約 (``BriefingListItem.latest``)。

    未生成カテゴリでは ``BriefingListItem.latest = None`` で表現する。
    一覧バンド表示用に見出し / summary / 件数を同梱する。詳細
    (``BriefingDetail``) と異なり chapters / keyArticles は持たない。
    """

    week_start: date
    headline: str = Field(max_length=MAX_BRIEFING_HEADLINE_LEN)
    summary: str = Field(max_length=MAX_BRIEFING_SUMMARY_LEN)
    input_article_count: int


class BriefingListItem(_CamelBase):
    """一覧 1 行: カテゴリ + 最新 briefing 参照 (未生成は None)。"""

    category: CategoryEmbed
    latest: BriefingSummary | None


class BriefingListResponse(_CamelBase):
    """``GET /api/v1/briefing`` のレスポンス。

    ``items`` は ``Category.id`` 昇順で 11 カテゴリ全部を返す。並び順は
    backend で確定し、frontend での sort を不要にする。``total_articles`` は
    ``current_week_start`` 週に生成された briefing の ``input_article_count``
    合計 (masthead「今週 N 件を解析」用、古い週の stale briefing は含めない)。
    """

    current_week_start: date
    total_articles: int
    items: list[BriefingListItem] = Field(max_length=_MAX_BRIEFING_LIST_ITEMS)
