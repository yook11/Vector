"""briefing 入力 article の読取と WeeklyBriefing の永続化 Repository。

読取側の週境界は JST (``WEEK_TZ``) 月曜 00:00 起点の date で受け、DB の
TIMESTAMPTZ (UTC) とは tz-aware datetime に変換して比較する。
commit は呼び出し側 (Service) の責務。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.domain.article import ArticleInput
from app.insights.briefing.domain.briefing import WeeklyBriefingContent
from app.insights.trend_discovery.domain.window import WEEK_TZ
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.weekly_briefing import WeeklyBriefing

# --- BriefingArticleRepository — briefing 入力 article の読取 ---


class BriefingArticleRepository:
    """Briefing 入力用の article 取得をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch(self, *, week_start: date, category_id: int) -> list[ArticleInput]:
        """指定週 × カテゴリの analysis 済 article を取得する。

        ``ArticleInput.id`` は公開 /news id 空間 (``AnalyzedArticleRecord.id``)。
        LLM 入出力・JSONB 永続化・response embed が同一 id 空間で揃う。

        Returns:
            id 昇順で安定ソートした ``ArticleInput`` のリスト。
            該当なしの場合は空リスト。
        """
        tz = ZoneInfo(WEEK_TZ)
        week_start_jst = datetime.combine(week_start, time(0, 0), tzinfo=tz)
        week_end_jst = week_start_jst + timedelta(days=7)

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
        return [
            ArticleInput(
                id=row.id,
                title_ja=row.translated_title,
                summary_ja=row.summary,
            )
            for row in rows
        ]


# --- BriefingRepository — WeeklyBriefing の永続化 ---


class BriefingRepository:
    """``weekly_briefings`` への CRUD をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, *, week_start: date, category_id: int) -> bool:
        """`try_advance_from` 用 cheap exists 判定。"""
        stmt = (
            select(WeeklyBriefing.id)
            .where(
                WeeklyBriefing.week_start_date == week_start,
                WeeklyBriefing.category_id == category_id,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def find_latest_by_category(
        self, *, category_id: int
    ) -> WeeklyBriefing | None:
        """指定カテゴリの最新 briefing 1 件を返す (なければ None)。

        ix_weekly_briefings_category_week が左端 + DESC で効く。
        """
        stmt = (
            select(WeeklyBriefing)
            .where(WeeklyBriefing.category_id == category_id)
            .order_by(WeeklyBriefing.week_start_date.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_latest_for_each_category(self) -> dict[int, WeeklyBriefing]:
        """category_id → 最新 briefing の dict を 1 クエリで返す。

        未生成カテゴリは entry なし (呼出側で ``dict.get(id)`` → ``None``)。
        PostgreSQL ``DISTINCT ON`` を使うことで、Python loop で N 回
        ``find_latest_by_category`` を叩くより SQL 1 回で完結する。
        """
        stmt = (
            select(WeeklyBriefing)
            .order_by(
                WeeklyBriefing.category_id,
                WeeklyBriefing.week_start_date.desc(),
            )
            .distinct(WeeklyBriefing.category_id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {b.category_id: b for b in rows}

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
        force: bool = False,
    ) -> WeeklyBriefing | None:
        """検証済み briefing 内容を ``weekly_briefings`` に永続化する。

        入口を ``WeeklyBriefingContent`` に限定し「domain 検証を通った内容だけが
        保存される」を型で保証する。VO → 行への写像は本 method の責務。
        ``force=False`` (default) は新規 INSERT のみで、race 敗北 (既存あり) は
        副作用なしに ``None`` を返す。``force=True`` は既存行を上書きし
        ``generated_at`` / ``updated_at`` を ``NOW()`` に更新する。
        """
        values = {
            "week_start_date": week_start,
            "category_id": category_id,
            "headline": content.headline,
            "summary": content.summary,
            "chapters": [c.model_dump() for c in content.chapters],
            # domain 語彙 article_id (LLM 契約) → 永続キー assessment_id の改名境界。
            # 値は公開 /news id 空間 (AnalyzedArticleRecord.id)。旧形 {article_id} 行
            # (AnalyzableArticleRecord.id 空間) とはキー名で構造的に判別する。
            "key_articles": [
                {"assessment_id": a.article_id, "significance": a.significance}
                for a in content.key_articles
            ],
            "watch_points": [w.model_dump() for w in content.watch_points],
            "model_name": model_name,
            "input_article_count": input_article_count,
        }
        if force:
            stmt = (
                pg_insert(WeeklyBriefing)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_weekly_briefing",
                    set_={
                        "headline": values["headline"],
                        "summary": values["summary"],
                        "chapters": values["chapters"],
                        "key_articles": values["key_articles"],
                        "watch_points": values["watch_points"],
                        "model_name": model_name,
                        "input_article_count": input_article_count,
                        "generated_at": func.now(),
                        "updated_at": func.now(),
                    },
                )
                .returning(WeeklyBriefing)
            )
        else:
            stmt = (
                pg_insert(WeeklyBriefing)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_weekly_briefing")
                .returning(WeeklyBriefing)
            )
        return (await self._session.execute(stmt)).scalar_one_or_none()
