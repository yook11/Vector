"""TrendsRepository の集計 SQL 境界条件テスト。

検証対象:
- ``get_trending_entities``: hot 判定 (continued trend / new burst の両条件)
- ``get_trending_topics``: 同条件、topic 単位
- ``get_new_entities``: 過去 lookback 週に出現履歴なし AND 現週 >=1
- ``count_source_analyses``: 現週の analysis 件数

境界として:
- 期間境界 (current_start ちょうど含む / current_end ちょうど除外)
- カテゴリ filter
- DISTINCT extraction_id (同一 extraction 内の重複 entity を 1 カウントに)
- previous=0 / NOT EXISTS (新規エンティティ)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.digest.config import NEW_ENTITY_LOOKBACK_WEEKS
from app.digest.repository.trends import TrendsRepository
from app.models.category import Category

from .conftest import SeedAnalysis

JST = ZoneInfo("Asia/Tokyo")
WEEK = timedelta(days=7)


def _jst(year: int, month: int, day: int, *, hour: int = 12) -> datetime:
    """JST 指定日付 (デフォルト 12:00) を tz-aware datetime として返す。"""
    return datetime(year, month, day, hour, tzinfo=JST)


# 基準週: 2026-04-13 (月) 00:00 JST から 2026-04-20 (月) 00:00 JST
WEEK_START = _jst(2026, 4, 13, hour=0)
WEEK_END = WEEK_START + WEEK
PREV_START = WEEK_START - WEEK
LOOKBACK_START = WEEK_START - WEEK * NEW_ENTITY_LOOKBACK_WEEKS


# ---------------------------------------------------------------------------
# get_trending_entities
# ---------------------------------------------------------------------------


class TestGetTrendingEntities:
    @pytest.mark.asyncio
    async def test_returns_continued_trend_entity(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """current >= 5 AND previous >= 2 は hot として返る。"""
        cat = sample_categories[0]
        for i in range(5):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=9 + i),
                entities=[("NVIDIA", "company")],
            )
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 7, hour=9 + i),
                entities=[("NVIDIA", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        trend = results[0]
        assert str(trend.name) == "NVIDIA"
        assert str(trend.type) == "company"
        assert trend.current_count == 5
        assert trend.previous_count == 2

    @pytest.mark.asyncio
    async def test_returns_new_burst_entity_without_previous(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """previous=0 でも current >= NEW_BURST_THRESHOLD なら hot。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                entities=[("DeepSeek", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert results[0].current_count == 10
        assert results[0].previous_count == 0

    @pytest.mark.asyncio
    async def test_excludes_below_min_current(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """current < MIN_CURRENT (=5) は除外。"""
        cat = sample_categories[0]
        for i in range(4):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                entities=[("NVIDIA", "company")],
            )
        for i in range(3):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 7, hour=i),
                entities=[("NVIDIA", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()

    @pytest.mark.asyncio
    async def test_excludes_low_previous_without_burst(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """current >= 5 だが previous < 2 かつ current < 10 は除外。"""
        cat = sample_categories[0]
        for i in range(7):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                entities=[("Edge", "concept")],
            )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 7, hour=9),
            entities=[("Edge", "concept")],
        )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()

    @pytest.mark.asyncio
    async def test_filters_by_category(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """異なる category_id の analysis は集計に含まれない。"""
        target = sample_categories[0]
        other = sample_categories[1]
        for i in range(10):
            await seed_analysis(
                category_id=other.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                entities=[("NVIDIA", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=target.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()

    @pytest.mark.asyncio
    async def test_respects_window_boundaries(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """current_start は含み、current_end は含まない (半開区間)。"""
        cat = sample_categories[0]
        # current_start ちょうど (含まれる)
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=WEEK_START,
            entities=[("Edge", "company")],
        )
        # current_end ちょうど (含まれない)
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=WEEK_END,
            entities=[("Edge", "company")],
        )
        # current 内 (含まれる) を 9 件追加して合計 10 件にする
        for i in range(9):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                entities=[("Edge", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert results[0].current_count == 10  # 11 件中 1 件は除外

    @pytest.mark.asyncio
    async def test_distinct_extraction_dedupes(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """同一 extraction 内に同 entity が複数出ても 1 カウント。

        ArticleEntity の重複行を seed しなくとも、entity の出現は extraction 単位で
        評価されることを確認する。
        """
        cat = sample_categories[0]
        # 5 件の独立 extraction で NVIDIA が登場
        for i in range(5):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                entities=[("NVIDIA", "company"), ("NVIDIA", "company")],  # 重複
            )
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 7, hour=i),
                entities=[("NVIDIA", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert results[0].current_count == 5  # 重複でなく extraction 数

    @pytest.mark.asyncio
    async def test_groups_case_insensitively(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """``NVIDIA`` と ``Nvidia`` は同一エンティティとして集約される (lower(name))。

        display 名は GROUP の MIN(name) (Postgres の locale 依存) を 1 つ採用する。
        どの casing が選ばれるかは実装の責務外で、casing が保持されること
        (= lowercase 化されない) のみ検証する。
        """
        cat = sample_categories[0]
        for i in range(3):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                entities=[("NVIDIA", "company")],
            )
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=10 + i),
                entities=[("Nvidia", "company")],
            )
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 7, hour=i),
                entities=[("nvidia", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        trend = results[0]
        assert trend.current_count == 5
        assert trend.previous_count == 2
        assert str(trend.name).lower() == "nvidia"
        assert str(trend.name) in {"NVIDIA", "Nvidia"}  # casing 保持 (lowercase でない)

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_data(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
    ) -> None:
        cat = sample_categories[0]
        repo = TrendsRepository(db_session)
        results = await repo.get_trending_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()


# ---------------------------------------------------------------------------
# get_trending_topics
# ---------------------------------------------------------------------------


class TestGetTrendingTopics:
    @pytest.mark.asyncio
    async def test_returns_continued_trend_topic(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        cat = sample_categories[0]
        for i in range(5):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                topic="ai agents",
            )
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 7, hour=i),
                topic="ai agents",
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_topics(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert str(results[0].topic) == "ai agents"
        assert results[0].current_count == 5
        assert results[0].previous_count == 2

    @pytest.mark.asyncio
    async def test_returns_new_burst_topic(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                topic="quantum computing",
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_topics(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert results[0].current_count == 10
        assert results[0].previous_count == 0

    @pytest.mark.asyncio
    async def test_excludes_below_threshold(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        cat = sample_categories[0]
        for i in range(7):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                topic="6g",
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_trending_topics(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()

    @pytest.mark.asyncio
    async def test_filters_by_category(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        target = sample_categories[0]
        other = sample_categories[1]
        for i in range(10):
            await seed_analysis(
                category_id=other.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                topic="quantum computing",
            )
        repo = TrendsRepository(db_session)
        results = await repo.get_trending_topics(
            category_id=target.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()


# ---------------------------------------------------------------------------
# get_new_entities
# ---------------------------------------------------------------------------


class TestGetNewEntities:
    @pytest.mark.asyncio
    async def test_returns_entity_absent_in_lookback(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """過去 lookback 週に出現履歴なし AND 現週 >=1 は new entity。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=9),
            entities=[("DeepSeek-R1", "product")],
        )

        repo = TrendsRepository(db_session)
        results = await repo.get_new_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            lookback_start=LOOKBACK_START,
        )
        assert len(results) == 1
        new_ent = results[0]
        assert str(new_ent.name) == "DeepSeek-R1"
        assert str(new_ent.type) == "product"
        assert new_ent.current_count == 1

    @pytest.mark.asyncio
    async def test_excludes_entity_with_lookback_history(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """過去 lookback 週内に 1 件でも出現していれば new ではない。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=9),
            entities=[("NVIDIA", "company")],
        )
        # lookback 内 (= current の 2 週前)
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 3, 30, hour=9),
            entities=[("NVIDIA", "company")],
        )

        repo = TrendsRepository(db_session)
        results = await repo.get_new_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            lookback_start=LOOKBACK_START,
        )
        assert results == ()

    @pytest.mark.asyncio
    async def test_includes_entity_outside_lookback_window(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """lookback 期間より古い出現履歴は new 判定に影響しない。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=9),
            entities=[("OldStartup", "company")],
        )
        # lookback_start (= current_start - 4 週) より前 = 5 週前
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=WEEK_START - WEEK * 5,
            entities=[("OldStartup", "company")],
        )

        repo = TrendsRepository(db_session)
        results = await repo.get_new_entities(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            lookback_start=LOOKBACK_START,
        )
        assert len(results) == 1
        assert str(results[0].name) == "OldStartup"

    @pytest.mark.asyncio
    async def test_filters_by_category_in_lookback(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """lookback の出現履歴は category 単位でしか参照しない。

        他カテゴリでは過去出現があっても、対象カテゴリの新規であれば new。
        """
        target = sample_categories[0]
        other = sample_categories[1]
        await seed_analysis(
            category_id=target.id,
            analyzed_at=_jst(2026, 4, 14, hour=9),
            entities=[("CrossCat", "company")],
        )
        await seed_analysis(
            category_id=other.id,
            analyzed_at=_jst(2026, 3, 30, hour=9),
            entities=[("CrossCat", "company")],
        )

        repo = TrendsRepository(db_session)
        results = await repo.get_new_entities(
            category_id=target.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            lookback_start=LOOKBACK_START,
        )
        assert len(results) == 1


# ---------------------------------------------------------------------------
# count_source_analyses
# ---------------------------------------------------------------------------


class TestCountSourceAnalyses:
    @pytest.mark.asyncio
    async def test_counts_all_categories_in_window(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """``count_source_analyses`` は全カテゴリ合算で数える (snapshot メタ情報用)。"""
        await seed_analysis(
            category_id=sample_categories[0].id,
            analyzed_at=_jst(2026, 4, 14),
        )
        await seed_analysis(
            category_id=sample_categories[1].id,
            analyzed_at=_jst(2026, 4, 15),
        )
        # window 外
        await seed_analysis(
            category_id=sample_categories[0].id,
            analyzed_at=_jst(2026, 4, 7),
        )

        repo = TrendsRepository(db_session)
        count = await repo.count_source_analyses(
            current_start=WEEK_START, current_end=WEEK_END
        )
        assert count == 2

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_analyses(
        self,
        db_session: AsyncSession,
    ) -> None:
        repo = TrendsRepository(db_session)
        count = await repo.count_source_analyses(
            current_start=WEEK_START, current_end=WEEK_END
        )
        assert count == 0
