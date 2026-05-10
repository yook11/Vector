"""WeeklyBriefing の永続化 Repository。

責務:
- ``exists``: cheap な exists 判定 (ReadyForBriefing.try_advance_from の
  precondition)
- ``find_latest_by_category``: API endpoint (latest) の主クエリ
- ``find_by``: race 読戻し / 既存確認用 (week_start, category_id) PK lookup
- ``save``: ``force=False`` で新規 INSERT のみ (race 敗北は ``None``)、
  ``force=True`` で既存上書き (UPSERT)

commit は呼び出し側 (Service) の責務。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.weekly_briefing import WeeklyBriefing


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
        """指定 (week, category) の briefing を取得する (race 読戻し用)。"""
        stmt = select(WeeklyBriefing).where(
            WeeklyBriefing.week_start_date == week_start,
            WeeklyBriefing.category_id == category_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def save(
        self,
        briefing: WeeklyBriefing,
        *,
        force: bool = False,
    ) -> WeeklyBriefing | None:
        """briefing を ``weekly_briefings`` に永続化する。

        Args:
            briefing: 永続化対象 (id は自動採番なので未設定で渡す)
            force: ``True`` のとき既存行を上書きし ``generated_at`` /
                ``updated_at`` を ``NOW()`` に更新する。``False`` (default) は
                新規 INSERT のみで、衝突時は副作用なしに ``None`` を返す。

        Returns:
            永続化成功時: 永続化後の ``WeeklyBriefing``
            race 敗北時 (force=False かつ既存あり): ``None``
        """
        values = {
            "week_start_date": briefing.week_start_date,
            "category_id": briefing.category_id,
            "headline": briefing.headline,
            "overview": briefing.overview,
            "stories": briefing.stories,
            "model_name": briefing.model_name,
            "input_article_count": briefing.input_article_count,
        }
        if force:
            stmt = (
                pg_insert(WeeklyBriefing)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_weekly_briefing",
                    set_={
                        "headline": briefing.headline,
                        "overview": briefing.overview,
                        "stories": briefing.stories,
                        "model_name": briefing.model_name,
                        "input_article_count": briefing.input_article_count,
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
