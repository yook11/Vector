from datetime import UTC, datetime, timedelta

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.models.article_analysis import ArticleAnalysis
from app.models.category import Category
from app.models.topic import Topic

SIDEBAR_RECENT_WINDOW = timedelta(hours=24)


class CategoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_categories(self) -> list[Row[tuple[int, str, str]]]:
        """slug 順で全カテゴリを取得する.

        (id, slug, name) の行を返す.
        """
        stmt = select(Category.id, Category.slug, Category.name).order_by(Category.slug)
        result = await self.session.execute(stmt)
        return list(result.all())

    async def fetch_topic_stats(
        self,
    ) -> list[Row[tuple[int, str, str, int]]]:
        """カテゴリ別にトピックごとの直近 24 時間の記事数を取得する.

        (category_id, name, label_ja, recent_count) の行を返す.
        24 時間以内に分類された記事がないトピックも recent_count=0 で含める.
        """
        cutoff = datetime.now(UTC) - SIDEBAR_RECENT_WINDOW
        stmt = (
            select(
                Topic.category_id,
                Topic.name,
                Topic.label_ja,
                func.count(ArticleAnalysis.id).label("recent_count"),
            )
            .outerjoin(
                ArticleAnalysis,
                (ArticleAnalysis.topic_id == Topic.id)
                & (ArticleAnalysis.analyzed_at > cutoff),
            )
            .group_by(Topic.category_id, Topic.id, Topic.name, Topic.label_ja)
            .order_by(Topic.name)
        )
        result = await self.session.execute(stmt)
        return list(result.all())

    async def fetch_category_article_counts(
        self,
    ) -> list[Row[tuple[int, int]]]:
        """カテゴリごとの直近 24 時間に分類された記事数を取得する.

        (category_id, recent_count) の行を返す.
        24 時間以内の分類がないカテゴリは結果に含まれない（呼び出し側で 0 を補完する）.
        """
        cutoff = datetime.now(UTC) - SIDEBAR_RECENT_WINDOW
        stmt = (
            select(
                Topic.category_id,
                func.count(ArticleAnalysis.id).label("recent_count"),
            )
            .join(ArticleAnalysis, ArticleAnalysis.topic_id == Topic.id)
            .where(ArticleAnalysis.analyzed_at > cutoff)
            .group_by(Topic.category_id)
        )
        result = await self.session.execute(stmt)
        return list(result.all())
