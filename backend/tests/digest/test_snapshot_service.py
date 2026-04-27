"""WeeklyTrendsSnapshotService の Generated / Skipped 挙動テスト。

検証ポイント:
- generate_for_week (新規生成): Generated を返し DB に snapshot を 1 行保存
- generate_for_week (既存あり, force=False): Skipped を返し既存行を保持
- generate_for_week (既存あり, force=True): Generated を返し既存行を上書き
- bundle 内容: 全カテゴリ 1 セクションずつ含み、hot 判定の通った VO のみ詰まる
- source_analysis_count: window 内の analysis 件数 (全カテゴリ合算)
- _completed_week_start_for: 与えた datetime に対し「直近完了週」の月曜日を返す
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.digest.application.snapshot import (
    Generated,
    Skipped,
    WeeklyTrendsSnapshotService,
)
from app.digest.config import DEFAULT_LIMIT
from app.digest.repository.snapshots import SnapshotRepository
from app.models.category import Category

from .conftest import SeedAnalysis

JST = ZoneInfo("Asia/Tokyo")
WEEK_START = date(2026, 4, 13)


def _jst(year: int, month: int, day: int, *, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=JST)


# ---------------------------------------------------------------------------
# generate_for_week — 新規生成
# ---------------------------------------------------------------------------


class TestGenerateForWeek:
    @pytest.mark.asyncio
    async def test_generates_snapshot_when_absent(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """既存なし: Generated を返し snapshot を 1 行保存する。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                topic="ai agents",
                entities=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = WeeklyTrendsSnapshotService(session_factory)
        result = await service.generate_for_week(WEEK_START)

        assert isinstance(result, Generated)
        assert result.week_start == WEEK_START
        assert result.source_analysis_count == 10

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_week(WEEK_START)
        assert snapshot is not None
        assert snapshot.source_analysis_count == 10
        assert snapshot.bundle["week_start"] == WEEK_START.isoformat()

    @pytest.mark.asyncio
    async def test_skips_when_existing_and_not_force(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """既存あり + force=False: Skipped を返し DB は更新されない。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                topic="ai agents",
                entities=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = WeeklyTrendsSnapshotService(session_factory)
        first = await service.generate_for_week(WEEK_START)
        assert isinstance(first, Generated)

        second = await service.generate_for_week(WEEK_START)
        assert isinstance(second, Skipped)
        assert second.week_start == WEEK_START

    @pytest.mark.asyncio
    async def test_overwrites_when_force(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """既存あり + force=True: Generated を返し既存行を上書きする。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                topic="ai agents",
                entities=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = WeeklyTrendsSnapshotService(session_factory)
        await service.generate_for_week(WEEK_START)

        # 追加 seed して再生成
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 15, hour=i),
                topic="ai agents",
                entities=[("NVIDIA", "company")],
            )
        await db_session.commit()

        result = await service.generate_for_week(WEEK_START, force=True)
        assert isinstance(result, Generated)
        assert result.source_analysis_count == 12

        # キャッシュを破棄して最新値を読む
        db_session.expire_all()
        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_week(WEEK_START)
        assert snapshot is not None
        assert snapshot.source_analysis_count == 12

    @pytest.mark.asyncio
    async def test_bundle_contains_all_categories(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """sections は全カテゴリを 1 つずつ含む (hot が無くても空セクションで残る)。"""
        for i in range(10):
            await seed_analysis(
                category_id=sample_categories[0].id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                entities=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = WeeklyTrendsSnapshotService(session_factory)
        await service.generate_for_week(WEEK_START)

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_week(WEEK_START)
        assert snapshot is not None
        sections = snapshot.bundle["sections"]
        assert len(sections) == len(sample_categories)
        category_ids = {s["category_id"] for s in sections}
        assert category_ids == {c.id for c in sample_categories}

    @pytest.mark.asyncio
    async def test_caps_each_list_at_default_limit(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """new_entities は ``DEFAULT_LIMIT`` 件で truncate される (上位 N 件のみ残す)。

        Phase 1A の new entity 集計は閾値が緩く (current_count >= 1)、現実データ
        では 1 カテゴリ 1000+ 件に膨らむ。snapshot 段階で上限を切ることで JSONB
        肥大化と UI 描画のノイズを構造的に防ぐ (Phase 1B の LLM 入力にも有利)。
        """
        cat = sample_categories[0]
        # DEFAULT_LIMIT (=20) を超える new entity を仕込む。
        # 各 entity に異なる count を持たせ、上位が確実に残るよう降順スコアにする。
        for i in range(25):
            for hour in range(25 - i):  # entity_0 が最多、entity_24 が最少
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 14 + (hour // 24), hour=hour % 24),
                    entities=[(f"entity_{i:02d}", "company")],
                )
        await db_session.commit()

        service = WeeklyTrendsSnapshotService(session_factory)
        await service.generate_for_week(WEEK_START)

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_week(WEEK_START)
        assert snapshot is not None
        section = next(
            s for s in snapshot.bundle["sections"] if s["category_id"] == cat.id
        )
        assert len(section["new_entities"]) == DEFAULT_LIMIT
        # 上位 (entity_00 = 25 件) が残っており、最下位 (entity_24 = 1 件) は落ちる
        names = [e["name"] for e in section["new_entities"]]
        assert "entity_00" in names
        assert "entity_24" not in names


# ---------------------------------------------------------------------------
# _completed_week_start_for (pure function)
# ---------------------------------------------------------------------------


class TestCompletedWeekStartFor:
    def test_monday_returns_previous_monday(self) -> None:
        """月曜日に呼ぶと前週月曜を返す (今週はまだ未完了)。"""
        now = datetime(2026, 4, 27, 0, 5, tzinfo=JST)  # JST 月曜 00:05
        result = WeeklyTrendsSnapshotService._completed_week_start_for(now)
        assert result == date(2026, 4, 20)

    def test_sunday_returns_two_weeks_back_monday(self) -> None:
        """日曜日に呼ぶとその週月曜の前週 (= 完了済み週) を返す。

        日曜は ``weekday()=6``。同じ週の月曜は 6 日前 (4/20)、その前週月曜が 4/13。
        """
        now = datetime(2026, 4, 26, 23, 50, tzinfo=JST)  # JST 日曜 23:50
        result = WeeklyTrendsSnapshotService._completed_week_start_for(now)
        assert result == date(2026, 4, 13)

    def test_wednesday_returns_previous_week_monday(self) -> None:
        now = datetime(2026, 4, 22, 12, 0, tzinfo=JST)  # JST 水曜
        result = WeeklyTrendsSnapshotService._completed_week_start_for(now)
        assert result == date(2026, 4, 13)


# ---------------------------------------------------------------------------
# generate_for_latest_completed_week
# ---------------------------------------------------------------------------


class TestGenerateForLatestCompletedWeek:
    @pytest.mark.asyncio
    async def test_delegates_to_completed_week_start(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
    ) -> None:
        """latest 経路は ``_completed_week_start_for(now)`` で算出した週で生成する。

        現在時刻は実時刻なので具体的な date は assert しない。Generated が返り、
        その week_start が JST 月曜であることを確認する。
        """
        service = WeeklyTrendsSnapshotService(session_factory)
        result = await service.generate_for_latest_completed_week()

        assert isinstance(result, Generated)
        # JST 月曜は weekday() == 0
        assert result.week_start.weekday() == 0
        # snapshot が DB に存在
        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_week(result.week_start)
        assert snapshot is not None

    @pytest.mark.asyncio
    async def test_skips_when_existing(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
    ) -> None:
        service = WeeklyTrendsSnapshotService(session_factory)
        first = await service.generate_for_latest_completed_week()
        assert isinstance(first, Generated)

        # 同じ week は Skipped になる
        second = await service.generate_for_latest_completed_week()
        assert isinstance(second, Skipped)
        assert second.week_start == first.week_start


# ---------------------------------------------------------------------------
# Outcome 型の構造
# ---------------------------------------------------------------------------


class TestOutcomeTypes:
    def test_generated_is_frozen(self) -> None:
        outcome = Generated(week_start=date(2026, 4, 13), source_analysis_count=10)
        with pytest.raises(AttributeError):
            outcome.source_analysis_count = 99  # type: ignore[misc]

    def test_skipped_is_frozen(self) -> None:
        outcome = Skipped(week_start=date(2026, 4, 13))
        with pytest.raises(AttributeError):
            outcome.week_start = date(2026, 4, 20)  # type: ignore[misc]

    def test_generated_and_skipped_are_distinct(self) -> None:
        gen = Generated(week_start=date(2026, 4, 13), source_analysis_count=10)
        skp = Skipped(week_start=date(2026, 4, 13))
        assert isinstance(gen, Generated)
        assert isinstance(skp, Skipped)
        assert not isinstance(gen, Skipped)
