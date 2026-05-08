from datetime import UTC, datetime, timedelta

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment

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

    async def fetch_category_article_counts(
        self,
    ) -> list[Row[tuple[int, int]]]:
        """カテゴリごとの直近 24 時間に分類された記事数を取得する.

        (category_id, recent_count) の行を返す.
        24 時間以内の分類がないカテゴリは結果に含まれない（呼び出し側で 0 を補完する）.
        新しい複合インデックス ix_article_analyses_category_id_analyzed_at が
        左端 + range で効く.
        """
        cutoff = datetime.now(UTC) - SIDEBAR_RECENT_WINDOW
        stmt = (
            select(
                InScopeAssessment.category_id,
                func.count(InScopeAssessment.id).label("recent_count"),
            )
            .where(InScopeAssessment.analyzed_at > cutoff)
            .group_by(InScopeAssessment.category_id)
        )
        result = await self.session.execute(stmt)
        return list(result.all())
