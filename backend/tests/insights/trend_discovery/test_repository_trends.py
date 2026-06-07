"""TrendsRepository の集計 SQL 境界条件テスト。

検証対象:
- ``get_ranked_mentions``: floor (current >= MIN_CURRENT) を通過した mention の
  pool を current/previous 件数つきで返す (hot ゲート・並べ替えは service の責務
  なのでここでは掛けない)。
- ``get_mention_key_points``: 指定 mention の現週 key_point content を記事レベル
  dedup して最大 2 本。
- ``get_related_mentions``: 指定 mention と同一 key_point 内で共起した別 mention を
  共起記事数 >= MIN_SHARED_ARTICLES で top3。
- ``count_source_analyses``: 現週の analysis 件数。

境界として:
- 期間境界 (current_start ちょうど含む / current_end ちょうど除外)
- カテゴリ filter
- DISTINCT assessment.id (同一 assessment 内の重複 mention を 1 カウントに)
- ``key_points IS NULL`` 行を集計対象外にする (PR 1 デプロイ前の旧行)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.trend_discovery.repository.trends import TrendsRepository
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


# get_ranked_mentions


class TestGetRankedMentions:
    @pytest.mark.asyncio
    async def test_returns_continued_trend_mention(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """current >= MIN_CURRENT は current/previous 件数つきで pool に入る。"""
        cat = sample_categories[0]
        for i in range(5):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=9 + i),
                mentions=[("NVIDIA", "company")],
            )
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 7, hour=9 + i),
                mentions=[("NVIDIA", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        trend = results[0]
        assert str(trend.name) == "NVIDIA"
        assert str(trend.type) == "company"
        assert trend.appearance_count == 5
        assert trend.previous_appearance_count == 2

    @pytest.mark.asyncio
    async def test_includes_floor_passing_without_hot_gate(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """current >= MIN_CURRENT なら previous<2 かつ current<burst でも pool に入る。

        旧 ``get_trending_entities`` は hot ゲート (previous>=2 OR current>=burst) を
        SQL WHERE で掛けていた。pool は出現回数ランキングの母集団でもあるため hot
        ゲートを外し、floor だけで残す (hot 判定は service の伸び率ランキング側)。
        """
        cat = sample_categories[0]
        for i in range(7):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("Edge", "technology")],
            )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 7, hour=9),
            mentions=[("Edge", "technology")],
        )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert str(results[0].name) == "Edge"
        assert results[0].appearance_count == 7
        assert results[0].previous_appearance_count == 1

    @pytest.mark.asyncio
    async def test_excludes_below_min_current(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """current < MIN_CURRENT (=5) は floor で除外。"""
        cat = sample_categories[0]
        for i in range(4):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("NVIDIA", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
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
                mentions=[("NVIDIA", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
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
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=WEEK_START,
            mentions=[("Edge", "company")],
        )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=WEEK_END,
            mentions=[("Edge", "company")],
        )
        for i in range(9):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("Edge", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert (
            results[0].appearance_count == 10
        )  # 11 件中 current_end ちょうどの 1 件は除外

    @pytest.mark.asyncio
    async def test_distinct_assessment_dedupes(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """同一 assessment 内に同 mention が複数出ても COUNT(DISTINCT a.id) で 1 件。"""
        cat = sample_categories[0]
        for i in range(5):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("NVIDIA", "company"), ("NVIDIA", "company")],  # 重複
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert results[0].appearance_count == 5  # 重複でなく assessment 数

    @pytest.mark.asyncio
    async def test_groups_case_insensitively(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """``NVIDIA`` と ``Nvidia`` は同一 mention として集約される (lower(name))。

        display 名は GROUP の MIN(name) を 1 つ採用する。どの casing が選ばれるかは
        実装の責務外で、casing が保持されること (lowercase 化されない) のみ検証する。
        """
        cat = sample_categories[0]
        for i in range(3):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("NVIDIA", "company")],
            )
        for i in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=10 + i),
                mentions=[("Nvidia", "company")],
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        trend = results[0]
        assert trend.appearance_count == 5
        assert str(trend.name).lower() == "nvidia"
        assert str(trend.name) in {"NVIDIA", "Nvidia"}  # casing 保持

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_data(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
    ) -> None:
        cat = sample_categories[0]
        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()

    @pytest.mark.asyncio
    async def test_excludes_rows_with_null_key_points(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """``key_points IS NULL`` 行 (PR 1 デプロイ前の旧行) は集計対象外。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                key_points_null=True,
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()

    @pytest.mark.asyncio
    async def test_handles_empty_key_points_array(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """``key_points = []`` 行は LATERAL で自然に 0 件、エラーにならない。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=(),
            )

        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert results == ()


# get_mention_key_points

_NVIDIA_KEY = ("nvidia", "company")


class TestGetMentionKeyPoints:
    @pytest.mark.asyncio
    async def test_returns_latest_first_max_two(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """最新優先で最大 2 本の content を返す (互いに離れた embedding)。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            content="oldest",
            mentions=[("NVIDIA", "company")],
            embedding=[1.0, 0.0, 0.0],
        )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=2),
            content="middle",
            mentions=[("NVIDIA", "company")],
            embedding=[0.0, 1.0, 0.0],
        )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=3),
            content="newest",
            mentions=[("NVIDIA", "company")],
            embedding=[0.0, 0.0, 1.0],
        )

        repo = TrendsRepository(db_session)
        result = await repo.get_mention_key_points(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        assert result[_NVIDIA_KEY] == ("newest", "middle")

    @pytest.mark.asyncio
    async def test_collapses_near_duplicate_articles(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """embedding が近接する別記事は同一トピックとして畳む。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=3),
            content="primary",
            mentions=[("NVIDIA", "company")],
            embedding=[1.0, 0.0],
        )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=2),
            content="near-dup",
            mentions=[("NVIDIA", "company")],
            embedding=[1.0, 0.02],  # primary と cosine 距離 < 0.1
        )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            content="distinct",
            mentions=[("NVIDIA", "company")],
            embedding=[0.0, 1.0],  # 直交 = 別トピック
        )

        repo = TrendsRepository(db_session)
        result = await repo.get_mention_key_points(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        assert result[_NVIDIA_KEY] == ("primary", "distinct")

    @pytest.mark.asyncio
    async def test_same_assessment_yields_one_content(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """同一記事内で entity が複数 key_point に出ても content は 1 本まで。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            key_points=[
                ("first key point", [("NVIDIA", "company")]),
                ("second key point", [("NVIDIA", "company")]),
            ],
            embedding=[1.0, 0.0],
        )

        repo = TrendsRepository(db_session)
        result = await repo.get_mention_key_points(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        contents = result[_NVIDIA_KEY]
        assert len(contents) == 1
        assert contents[0] in {"first key point", "second key point"}

    @pytest.mark.asyncio
    async def test_null_embedding_treated_as_distinct(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """embedding が NULL の旧行は近接判定をスキップし別記事として残る。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=2),
            content="legacy-a",
            mentions=[("NVIDIA", "company")],
            embedding=None,
        )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            content="legacy-b",
            mentions=[("NVIDIA", "company")],
            embedding=None,
        )

        repo = TrendsRepository(db_session)
        result = await repo.get_mention_key_points(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        assert result[_NVIDIA_KEY] == ("legacy-a", "legacy-b")

    @pytest.mark.asyncio
    async def test_excludes_other_category(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """別カテゴリの key_point は対象 mention でも返らない。"""
        target = sample_categories[0]
        other = sample_categories[1]
        await seed_analysis(
            category_id=other.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            content="other-cat",
            mentions=[("NVIDIA", "company")],
            embedding=[1.0, 0.0],
        )

        repo = TrendsRepository(db_session)
        result = await repo.get_mention_key_points(
            category_id=target.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_mention_keys_skips_query(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """mention_keys が空ならクエリせず {} を返す。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            mentions=[("NVIDIA", "company")],
            embedding=[1.0, 0.0],
        )

        repo = TrendsRepository(db_session)
        result = await repo.get_mention_key_points(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[],
        )
        assert result == {}


# get_related_mentions


class TestGetRelatedMentions:
    @pytest.mark.asyncio
    async def test_returns_co_mention_above_min_shared(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """同一 key_point で 2 記事以上共起した相手を共起記事数つきで返す。"""
        cat = sample_categories[0]
        for hour in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("NVIDIA", "company"), ("OpenAI", "company")],
            )

        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        related = result[_NVIDIA_KEY]
        assert len(related) == 1
        assert str(related[0].name) == "OpenAI"
        assert related[0].shared_article_count == 2

    @pytest.mark.asyncio
    async def test_excludes_single_co_occurrence(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """共起が 1 記事のみ (< MIN_SHARED_ARTICLES) は除外。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            mentions=[("NVIDIA", "company"), ("OpenAI", "company")],
        )

        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_different_key_point_not_counted(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """別 key_point に居る mention は共起としてカウントしない。"""
        cat = sample_categories[0]
        for hour in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                key_points=[
                    ("anchor kp", [("NVIDIA", "company")]),
                    ("other kp", [("OpenAI", "company")]),
                ],
            )

        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_excludes_self_pair(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """anchor 自身は共起相手に含めない (相手は OpenAI のみ)。"""
        cat = sample_categories[0]
        for hour in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[
                    ("NVIDIA", "company"),
                    ("NVIDIA", "company"),
                    ("OpenAI", "company"),
                ],
            )

        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        names = {str(r.name) for r in result[_NVIDIA_KEY]}
        assert names == {"OpenAI"}

    @pytest.mark.asyncio
    async def test_top_three_by_shared_count(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """共起記事数降順 top3 (4 件目は落ち、同数は match_key 昇順)。"""
        cat = sample_categories[0]
        peers = [("OpenAI", 4), ("Google", 3), ("Meta", 2), ("Anthropic", 2)]
        hour = 0
        for peer, count in peers:
            for _ in range(count):
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 14, hour=hour),
                    mentions=[("NVIDIA", "company"), (peer, "company")],
                )
                hour += 1

        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        related = result[_NVIDIA_KEY]
        # OpenAI(4) > Google(3) > {Anthropic,Meta}(2) は match_key 昇順で Anthropic。
        assert [(str(r.name), r.shared_article_count) for r in related] == [
            ("OpenAI", 4),
            ("Google", 3),
            ("Anthropic", 2),
        ]

    @pytest.mark.asyncio
    async def test_casing_representative_for_co_mention(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """共起相手は lower で名寄せし、display は casing を保持した代表を採る。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            mentions=[("NVIDIA", "company"), ("OpenAI", "company")],
        )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=2),
            mentions=[("NVIDIA", "company"), ("openai", "company")],
        )

        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        related = result[_NVIDIA_KEY]
        assert len(related) == 1
        assert related[0].shared_article_count == 2
        assert str(related[0].name).lower() == "openai"
        assert str(related[0].name) in {"OpenAI", "openai"}  # casing 保持


# count_source_analyses


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
