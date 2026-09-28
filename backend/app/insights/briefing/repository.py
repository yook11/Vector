"""briefing の Ready 構築に使う事実の読取と、WeeklyBriefing の永続化 Repository。

週は月曜の date で受け、DB の TIMESTAMPTZ とは ``week_bounds`` の tz-aware な
期間で比較する。commit は呼び出し側の責務。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.domain.briefing import WeeklyBriefingContent, week_bounds
from app.insights.briefing.domain.ready import BriefingArticle, BriefingReadyBuildFacts
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.category import Category
from app.models.weekly_briefing import WeeklyBriefing


class BriefingRepository:
    """``weekly_briefings`` への CRUD をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_ready_build_facts(
        self, *, week_start: date, category_id: int
    ) -> BriefingReadyBuildFacts | None:
        """カテゴリ・保存済みか・対象週の記事を読む (カテゴリが無ければ None)。"""
        category = (
            await self._session.execute(
                select(Category.slug, Category.name).where(Category.id == category_id)
            )
        ).one_or_none()
        if category is None:
            return None
        return BriefingReadyBuildFacts(
            category_slug=str(category.slug),
            category_name=str(category.name),
            already_generated=await self.exists(
                week_start=week_start, category_id=category_id
            ),
            articles=await self._fetch_week_articles(
                week_start=week_start, category_id=category_id
            ),
        )

    async def _fetch_week_articles(
        self, *, week_start: date, category_id: int
    ) -> tuple[BriefingArticle, ...]:
        """id は公開記事 id (AnalyzedArticleRecord.id) で、LLM 入出力・JSONB・
        閲覧 API の embed が同じ id 空間で揃う。
        """
        week_start_jst, week_end_jst = week_bounds(week_start)

        stmt = (
            select(
                AnalyzedArticleRecord.id,
                AnalyzedArticleRecord.translated_title,
                AnalyzedArticleRecord.summary,
            )
            .where(
                AnalyzedArticleRecord.category_id == category_id,
                AnalyzedArticleRecord.analyzed_at >= week_start_jst,
                AnalyzedArticleRecord.analyzed_at < week_end_jst,
            )
            .order_by(AnalyzedArticleRecord.id)
        )
        rows = (await self._session.execute(stmt)).all()
        return tuple(
            BriefingArticle(
                analyzed_article_id=row.id,
                translated_title=row.translated_title,
                summary=row.summary,
            )
            for row in rows
        )

    async def exists(self, *, week_start: date, category_id: int) -> bool:
        """同じ週・カテゴリの briefing が保存済みか。"""
        stmt = (
            select(WeeklyBriefing.id)
            .where(
                WeeklyBriefing.week_start_date == week_start,
                WeeklyBriefing.category_id == category_id,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def find_by(
        self, *, week_start: date, category_id: int
    ) -> WeeklyBriefing | None:
        """指定 (week, category) の briefing を取得する。"""
        stmt = select(WeeklyBriefing).where(
            WeeklyBriefing.week_start_date == week_start,
            WeeklyBriefing.category_id == category_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def save(
        self,
        content: WeeklyBriefingContent,
        *,
        week_start: date,
        category_id: int,
        model_name: str,
        input_article_count: int,
    ) -> WeeklyBriefing | None:
        """検証済み briefing 内容を ``weekly_briefings`` に永続化する。

        入口を ``WeeklyBriefingContent`` に限定し「domain 検証を通った内容だけが
        保存される」を型で保証する。VO → 行への写像は本 method の責務。
        新規 INSERT のみで、race 敗北 (既存あり) は副作用なしに ``None`` を返す。
        """
        values = {
            "week_start_date": week_start,
            "category_id": category_id,
            "headline": content.headline,
            "summary": content.summary,
            "chapters": [c.model_dump() for c in content.chapters],
            "key_articles": [
                {
                    "analyzed_article_id": a.analyzed_article_id,
                    "significance": a.significance,
                }
                for a in content.key_articles
            ],
            "watch_points": [w.model_dump() for w in content.watch_points],
            "model_name": model_name,
            "input_article_count": input_article_count,
        }
        stmt = (
            pg_insert(WeeklyBriefing)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_weekly_briefing")
            .returning(WeeklyBriefing)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
