"""WeeklyTrendsSnapshotService.execute の挙動テスト (Phase 4)。

検証する観点:
- execute(ready, force=False) 正常系: Generated を返し snapshot を 1 行保存
- execute(ready, force=True) 既存上書き: Generated を返し source_analysis_count
  を反映 (`generated_at` も更新)
- bundle 内容: 全カテゴリ 1 セクションずつ含み、hot 判定の通った VO のみ詰まる
- source_analysis_count: window 内の analysis 件数 (全カテゴリ合算)
- DEFAULT_LIMIT で truncate
- race 敗北 (force=False で同時 INSERT 競合): find_by_week で読戻し → Generated
  合流
- winner missing (find_by_week でも None): RuntimeError 伝播

Phase 4 で skip 経路は ``ReadyForDigest.try_advance_from`` 側に移管されたため、
Service.execute から ``Skipped`` Outcome は消えている。
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.digest.application.snapshot import Generated, WeeklyTrendsSnapshotService
from app.digest.config import DEFAULT_LIMIT
from app.digest.domain.ready import ReadyForDigest
from app.digest.repository.snapshots import SnapshotRepository
from app.models.category import Category

from .conftest import SeedAnalysis

JST = ZoneInfo("Asia/Tokyo")
WEEK_START = date(2026, 4, 13)


def _ready(week_start: date = WEEK_START, *, force: bool = False) -> ReadyForDigest:
    return ReadyForDigest(week_start=week_start, force=force)


def _jst(year: int, month: int, day: int, *, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=JST)


# ---------------------------------------------------------------------------
# execute — 新規生成
# ---------------------------------------------------------------------------


class TestExecute:
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
        result = await service.execute(_ready())

        assert isinstance(result, Generated)
        assert result.week_start == WEEK_START
        assert result.source_analysis_count == 10

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_week(WEEK_START)
        assert snapshot is not None
        assert snapshot.source_analysis_count == 10
        assert snapshot.bundle["week_start"] == WEEK_START.isoformat()

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
        await service.execute(_ready())

        # 追加 seed して再生成
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 15, hour=i),
                topic="ai agents",
                entities=[("NVIDIA", "company")],
            )
        await db_session.commit()

        result = await service.execute(_ready(force=True))
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
        await service.execute(_ready())

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
        肥大化と UI 描画のノイズを構造的に防ぐ。
        """
        cat = sample_categories[0]
        for i in range(25):
            for hour in range(25 - i):
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 14 + (hour // 24), hour=hour % 24),
                    entities=[(f"entity_{i:02d}", "company")],
                )
        await db_session.commit()

        service = WeeklyTrendsSnapshotService(session_factory)
        await service.execute(_ready())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_week(WEEK_START)
        assert snapshot is not None
        section = next(
            s for s in snapshot.bundle["sections"] if s["category_id"] == cat.id
        )
        assert len(section["new_entities"]) == DEFAULT_LIMIT
        names = [e["name"] for e in section["new_entities"]]
        assert "entity_00" in names
        assert "entity_24" not in names


# ---------------------------------------------------------------------------
# race-loss: save が None → find_by_week 読戻し → Generated 合流
# ---------------------------------------------------------------------------


class TestRaceLoss:
    @pytest.mark.asyncio
    async def test_reads_back_winner_when_save_returns_none(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
    ) -> None:
        """save が None (race 敗北) → find_by_week で勝者読戻し → Generated 合流。

        SnapshotRepository.save を ``None`` 戻りに patch し、別経路で実 row を
        投入しておく。Service が読戻し → Generated を返すことを検証する。
        """
        # 先に勝者 snapshot を投入する (別 worker が先行 INSERT したことの代理)
        async with session_factory() as session:
            repo = SnapshotRepository(session)
            from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot

            winner = WeeklyTrendsSnapshot(
                week_start=WEEK_START,
                bundle={"week_start": WEEK_START.isoformat(), "sections": []},
                source_analysis_count=0,
            )
            await repo.save(winner)
            await session.commit()

        service = WeeklyTrendsSnapshotService(session_factory)
        with patch.object(SnapshotRepository, "save", new=AsyncMock(return_value=None)):
            result = await service.execute(_ready())

        assert isinstance(result, Generated)
        assert result.week_start == WEEK_START

    @pytest.mark.asyncio
    async def test_raises_when_race_winner_missing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
    ) -> None:
        """save も find_by_week も None → RuntimeError 伝播 (異常系の見える化)。"""
        service = WeeklyTrendsSnapshotService(session_factory)
        with (
            patch.object(SnapshotRepository, "save", new=AsyncMock(return_value=None)),
            patch.object(
                SnapshotRepository, "find_by_week", new=AsyncMock(return_value=None)
            ),
            pytest.raises(RuntimeError, match="digest_race_winner_missing"),
        ):
            await service.execute(_ready())


# ---------------------------------------------------------------------------
# Outcome 型の構造
# ---------------------------------------------------------------------------


class TestOutcomeTypes:
    def test_generated_is_frozen(self) -> None:
        outcome = Generated(week_start=date(2026, 4, 13), source_analysis_count=10)
        with pytest.raises(AttributeError):
            outcome.source_analysis_count = 99  # type: ignore[misc]
