"""TrendDiscoveryService.execute の挙動テスト。

検証する観点:
- execute(ready, force=False) 正常系: TrendDiscoveryCompleted を返し
  snapshot を 1 行保存
- 集計対象記事 0 件: snapshot を保存せず SkippedNoTargetArticles を返す
- execute(ready, force=True) 既存上書き: TrendDiscoveryCompleted を返し
  source_analysis_count を反映 (`generated_at` も更新)
- bundle 内容: 全カテゴリ 1 セクションずつ含み、hot 判定の通った VO のみ詰まる
- source_analysis_count: window 内の analysis 件数 (全カテゴリ合算)
- MAX_TRENDS_PER_CATEGORY で truncate
- race 敗北 (force=False で同時 INSERT 競合): 読み戻しせず
  TrendDiscoveryConflict を返す

既存 snapshot skip は ``ReadyForTrendDiscovery.try_advance_from`` 側に移管されている。
一方、集計対象記事 0 件は Service が ``SkippedNoTargetArticles`` として返す。

集計窓は rolling 7d で半開区間 ``[window_end - 7d, window_end)`` を取る。
window_end = 2026-04-20 のとき、window = [2026-04-13 0:00 JST, 2026-04-20 0:00 JST)。
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insights.trend_discovery.application.service import (
    SkippedNoTargetArticles,
    TrendDiscoveryCompleted,
    TrendDiscoveryConflict,
    TrendDiscoveryService,
)
from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery
from app.insights.trend_discovery.domain.trend import MAX_TRENDS_PER_CATEGORY
from app.insights.trend_discovery.repository.snapshots import (
    SnapshotRepository,
    SnapshotSaveResult,
    SnapshotSaveStatus,
)
from app.models.category import Category

from .conftest import SeedAnalysis

JST = ZoneInfo("Asia/Tokyo")
WINDOW_END = date(2026, 4, 20)


def _ready(
    window_end: date = WINDOW_END, *, force: bool = False
) -> ReadyForTrendDiscovery:
    return ReadyForTrendDiscovery(window_end=window_end, force=force)


def _jst(year: int, month: int, day: int, *, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=JST)


# execute — 新規生成


class TestExecute:
    @pytest.mark.asyncio
    async def test_skips_without_saving_when_no_target_articles(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
    ) -> None:
        """window 内の対象記事が 0 件なら category 集計も snapshot 保存も行わない。"""
        service = TrendDiscoveryService(session_factory)
        with patch.object(
            TrendDiscoveryService,
            "_fetch_categories",
            new=AsyncMock(side_effect=AssertionError("category fetch not expected")),
        ):
            result = await service.execute(_ready())

        assert isinstance(result, SkippedNoTargetArticles)
        assert result.window_end == WINDOW_END
        assert result.source_analysis_count == 0
        assert result.completed_category_count is None

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is None
        assert sample_categories

    @pytest.mark.asyncio
    async def test_generates_snapshot_when_absent(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """既存なし: TrendDiscoveryCompleted を返し snapshot を 1 行保存する。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        result = await service.execute(_ready())

        assert isinstance(result, TrendDiscoveryCompleted)
        assert result.window_end == WINDOW_END
        assert result.source_analysis_count == 10
        assert result.completed_category_count == len(sample_categories)
        assert result.updated is False

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        assert snapshot.source_analysis_count == 10
        assert snapshot.bundle["window_end"] == WINDOW_END.isoformat()

    @pytest.mark.asyncio
    async def test_overwrites_when_force(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """既存あり + force=True: TrendDiscoveryCompleted を返し既存行を上書きする。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_ready())

        # 追加 seed して再生成
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 15, hour=i),
                mentions=[("NVIDIA", "company")],
            )
        await db_session.commit()

        result = await service.execute(_ready(force=True))
        assert isinstance(result, TrendDiscoveryCompleted)
        assert result.source_analysis_count == 12
        assert result.updated is True

        # キャッシュを破棄して最新値を読む
        db_session.expire_all()
        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
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
                mentions=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_ready())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
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
        """new_entities は ``MAX_TRENDS_PER_CATEGORY`` 件で truncate される。

        new entity 集計は閾値が緩く (current_count >= 1)、現実データでは 1 カテゴリ
        1000+ 件に膨らむ。snapshot 段階で上限を切ることで JSONB 肥大化と UI 描画の
        ノイズを構造的に防ぐ。
        """
        cat = sample_categories[0]
        for i in range(25):
            for hour in range(25 - i):
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 14 + (hour // 24), hour=hour % 24),
                    mentions=[(f"entity_{i:02d}", "company")],
                )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_ready())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        section = next(
            s for s in snapshot.bundle["sections"] if s["category_id"] == cat.id
        )
        assert len(section["new_entities"]) == MAX_TRENDS_PER_CATEGORY
        names = [e["name"] for e in section["new_entities"]]
        assert "entity_00" in names
        assert "entity_24" not in names


# race-loss: save が CONFLICT → 読み戻しせず TrendDiscoveryConflict


class TestRaceLoss:
    @pytest.mark.asyncio
    async def test_returns_conflict_without_winner_readback(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """save が CONFLICT (race 敗北) なら勝者を読まず conflict outcome にする。"""
        await seed_analysis(
            category_id=sample_categories[0].id,
            analyzed_at=_jst(2026, 4, 14),
            mentions=[("NVIDIA", "company")],
        )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        with (
            patch.object(
                SnapshotRepository,
                "save",
                new=AsyncMock(
                    return_value=SnapshotSaveResult(
                        status=SnapshotSaveStatus.CONFLICT,
                        snapshot=None,
                    )
                ),
            ),
            patch.object(
                SnapshotRepository,
                "find_by_window_end",
                new=AsyncMock(return_value=None),
            ) as find_by_window_end,
        ):
            result = await service.execute(_ready())

        assert isinstance(result, TrendDiscoveryConflict)
        assert result.window_end == WINDOW_END
        assert result.source_analysis_count == 1
        assert result.completed_category_count == len(sample_categories)
        find_by_window_end.assert_not_awaited()


# Outcome 型の構造


class TestOutcomeTypes:
    def test_completed_is_frozen(self) -> None:
        outcome = TrendDiscoveryCompleted(
            window_end=date(2026, 4, 20),
            source_analysis_count=10,
            completed_category_count=3,
        )
        with pytest.raises(AttributeError):
            outcome.source_analysis_count = 99  # type: ignore[misc]

    def test_skipped_no_target_articles_is_frozen(self) -> None:
        outcome = SkippedNoTargetArticles(window_end=date(2026, 4, 20))
        with pytest.raises(AttributeError):
            outcome.source_analysis_count = 99  # type: ignore[misc]

    def test_conflict_is_frozen(self) -> None:
        outcome = TrendDiscoveryConflict(
            window_end=date(2026, 4, 20),
            source_analysis_count=10,
            completed_category_count=3,
        )
        with pytest.raises(AttributeError):
            outcome.completed_category_count = 99  # type: ignore[misc]
