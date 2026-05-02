"""Briefing 入力用の analysis 済 article 取得 Repository。

責務:
- 指定 (week_start, category_id) の analysis を JST 週境界で抽出
- LLM に渡す ``ArticleInput`` (id + title_ja + summary_ja) のみを返す

時間境界:
- ``week_start`` は JST 月曜 00:00 起点の date
- analyzed_at が ``[week_start, week_start + 7 days)`` の範囲を含む
- DB は TIMESTAMPTZ (UTC) なので、JST 境界を tz-aware datetime に変換して
  比較する
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.domain.article import ArticleInput
from app.insights.snapshot.config import WEEK_TZ
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction


class BriefingArticleRepository:
    """Briefing 入力用の article 取得をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch(self, *, week_start: date, category_id: int) -> list[ArticleInput]:
        """指定週 × カテゴリの analysis 済 article を取得する。

        Returns:
            ``article_id`` 昇順で安定ソートした ``ArticleInput`` のリスト。
            該当なしの場合は空リスト。
        """
        tz = ZoneInfo(WEEK_TZ)
        week_start_jst = datetime.combine(week_start, time(0, 0), tzinfo=tz)
        week_end_jst = week_start_jst + timedelta(days=7)

        stmt = (
            select(
                ArticleExtraction.article_id,
                ArticleAnalysis.translated_title,
                ArticleAnalysis.summary,
            )
            .join(
                ArticleExtraction,
                ArticleAnalysis.extraction_id == ArticleExtraction.id,
            )
            .where(
                ArticleAnalysis.category_id == category_id,
                ArticleAnalysis.analyzed_at >= week_start_jst,
                ArticleAnalysis.analyzed_at < week_end_jst,
            )
            .order_by(ArticleExtraction.article_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            ArticleInput(
                id=row.article_id,
                title_ja=row.translated_title,
                summary_ja=row.summary,
            )
            for row in rows
        ]
