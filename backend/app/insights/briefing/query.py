"""表示専用のため Repository とドメインを経由せず、
SQL で取得してレスポンス型に組み立てる。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from pydantic import Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import NotFoundError
from app.insights.briefing.domain.briefing import (
    MAX_KEY_ARTICLES_PER_BRIEFING,
    latest_completed_week_start,
    now_in_jst,
)
from app.insights.briefing.schemas import (
    BriefingDetail,
    BriefingListItem,
    BriefingListResponse,
    BriefingResponse,
    BriefingSummary,
    EmptyBriefing,
    _BriefingArticleEmbed,
    _BriefingChapter,
    _BriefingKeyArticle,
)
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource
from app.models.weekly_briefing import WeeklyBriefing
from app.schemas import category as category_schema
from app.schemas.embeds import NewsSourceEmbed
from app.services.articles import extract_key_point_contents

# F10: DB に直書きされた巨大 key_articles で記事取得が膨らまないよう、
# 件数だけ取得前に検証する。
_KEY_ARTICLES_COUNT_GUARD: TypeAdapter[list[object]] = TypeAdapter(
    Annotated[list[object], Field(max_length=MAX_KEY_ARTICLES_PER_BRIEFING)]
)


def _to_category(category: Category) -> category_schema.Category:
    return category_schema.Category(slug=category.slug, name=category.name)


class BriefingQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_latest(self) -> BriefingListResponse:
        current_week_start = latest_completed_week_start(now_in_jst())
        categories = await self._fetch_categories()
        latest_by_category = await self._fetch_latest_summary_for_each_category()

        items: list[BriefingListItem] = []
        for category in categories:
            items.append(
                BriefingListItem(
                    category=_to_category(category),
                    latest=latest_by_category.get(category.id),
                )
            )
        return BriefingListResponse(current_week_start=current_week_start, items=items)

    async def get_latest(self, category_slug: str) -> BriefingResponse:
        category = await self._fetch_category(category_slug)
        if category is None:
            raise NotFoundError("Category not found")
        briefing = await self._fetch_latest_briefing(category.id)
        if briefing is None:
            return EmptyBriefing(category=_to_category(category))

        chapters = [_BriefingChapter.model_validate(c) for c in briefing.chapters]
        _KEY_ARTICLES_COUNT_GUARD.validate_python(briefing.key_articles)
        embeds = await self._fetch_article_embeds(
            {a["analyzed_article_id"] for a in briefing.key_articles}
        )
        # 記事を削除する経路がないため欠落しない前提で、
        # None なら ValidationError (500) として表に出す。
        key_articles = [
            _BriefingKeyArticle(
                significance=a["significance"],
                article=embeds.get(  # pyright: ignore[reportArgumentType]
                    a["analyzed_article_id"]
                ),
            )
            for a in briefing.key_articles
        ]
        watch_points = [w["statement"] for w in briefing.watch_points]

        return BriefingDetail(
            week_start=briefing.week_start_date,
            generated_at=briefing.generated_at,
            category=_to_category(category),
            headline=briefing.headline,
            summary=briefing.summary,
            chapters=chapters,
            key_articles=key_articles,
            watch_points=watch_points,
        )

    # --- SQL ---

    async def _fetch_categories(self) -> Sequence[Category]:
        stmt = select(Category).order_by(Category.id)
        return (await self._session.execute(stmt)).scalars().all()

    async def _fetch_category(self, slug: str) -> Category | None:
        stmt = select(Category).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _fetch_latest_briefing(self, category_id: int) -> WeeklyBriefing | None:
        stmt = (
            select(WeeklyBriefing)
            .where(WeeklyBriefing.category_id == category_id)
            .order_by(WeeklyBriefing.week_start_date.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _fetch_latest_summary_for_each_category(
        self,
    ) -> dict[int, BriefingSummary]:
        stmt = (
            select(
                WeeklyBriefing.category_id,
                WeeklyBriefing.week_start_date,
                WeeklyBriefing.headline,
                WeeklyBriefing.summary,
            )
            .order_by(
                WeeklyBriefing.category_id,
                WeeklyBriefing.week_start_date.desc(),
            )
            .distinct(WeeklyBriefing.category_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {
            row.category_id: BriefingSummary(
                week_start=row.week_start_date,
                headline=row.headline,
                summary=row.summary,
            )
            for row in rows
        }

    async def _fetch_article_embeds(
        self, analyzed_article_ids: set[int]
    ) -> dict[int, _BriefingArticleEmbed]:
        """JSONB の analyzed_article_id は公開 id (AnalyzedArticleRecord.id) と
        同じ空間なので、そのまま dict のキーにする。
        """
        if not analyzed_article_ids:
            return {}
        stmt = (
            select(
                AnalyzedArticleRecord.id,
                AnalyzedArticleRecord.translated_title,
                AnalyzedArticleRecord.key_points,
                AnalyzableArticleRecord.source_url,
                AnalyzableArticleRecord.published_at,
                NewsSource.name,
                NewsSource.attribution_label,
            )
            .join(
                ArticleCuration, ArticleCuration.id == AnalyzedArticleRecord.curation_id
            )
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .join(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
            .where(AnalyzedArticleRecord.id.in_(analyzed_article_ids))
        )
        rows = (await self._session.execute(stmt)).all()
        return {
            row.id: _BriefingArticleEmbed(
                id=row.id,
                translated_title=row.translated_title,
                source=NewsSourceEmbed(
                    name=row.name,
                    attribution_label=row.attribution_label,
                ),
                url=str(row.source_url),
                published_at=row.published_at,
                key_points=extract_key_point_contents(row.key_points),
            )
            for row in rows
        }
