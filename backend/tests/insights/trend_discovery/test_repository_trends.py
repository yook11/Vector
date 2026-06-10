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

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.insights.trend_discovery.domain.mention_name import MentionName
from app.insights.trend_discovery.domain.trend import MIN_CURRENT
from app.insights.trend_discovery.repository import (
    TrendsRepository,
    _match_key_expr,
)
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


# #3-A 名寄せキーの空白正規化 (読取 SQL を書込側 collapse に揃える)

# 名寄せキーの spec 上の正規形 ("Open  AI" / "Open\tAI" 等は collapse して同一)。
_OPEN_AI_KEY = ("open ai", "company")


class TestWhitespaceNameKeyNormalization:
    """surface が内部に連続空白・タブ・改行を持つ legacy 行でも、読取 SQL が

    書込側 (normalize_mention_surface / MentionName) と同じ collapse 規則で名寄せ
    することを固定する。これが外れると enrich の IN フィルタが外れ keyPoints /
    relatedMentions が silent に空になり、appearance_count も表記揺れで分裂する。
    """

    @pytest.mark.parametrize("surface", ["Open  AI", "Open\tAI", "Open\nAI"])
    @pytest.mark.asyncio
    async def test_key_points_populate_for_internal_whitespace_surface(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
        surface: str,
    ) -> None:
        """内部空白入り surface でも collapse 後キーで key_point を引ける。"""
        cat = sample_categories[0]
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 14, hour=1),
            key_points=[("kp body", [(surface, "company")])],
        )
        repo = TrendsRepository(db_session)
        result = await repo.get_mention_key_points(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_OPEN_AI_KEY],
        )
        assert result.get(_OPEN_AI_KEY) == ("kp body",)

    @pytest.mark.asyncio
    async def test_related_populates_for_internal_whitespace_anchor(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """anchor surface が内部空白を持っても collapse 後キーで共起を引ける。"""
        cat = sample_categories[0]
        for hour in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("Open  AI", "company"), ("NVIDIA", "company")],
            )
        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_OPEN_AI_KEY],
        )
        related = result.get(_OPEN_AI_KEY, ())
        assert len(related) == 1
        assert str(related[0].name) == "NVIDIA"
        assert related[0].shared_article_count == 2

    @pytest.mark.asyncio
    async def test_ranked_count_not_split_by_whitespace_variants(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """同一エンティティの表記揺れは 1 group に merge し count を分裂させない。"""
        cat = sample_categories[0]
        # double-space と single-space を分けて seed。合算が floor (MIN_CURRENT) に
        # 達するため、merge されなければ各群とも floor 未満で pool から落ちる。
        double_n = 3
        single_n = MIN_CURRENT - double_n
        for hour in range(double_n):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("Open  AI", "company")],
            )
        for hour in range(double_n, double_n + single_n):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("Open AI", "company")],
            )
        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert len(results) == 1
        assert str(results[0].name) == "Open AI"
        assert results[0].appearance_count == double_n + single_n

    @pytest.mark.asyncio
    async def test_whitespace_variants_excluded_as_self_pair(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """表記揺れの同一エンティティは collapse 後キーが一致し自己ペア除外される。"""
        cat = sample_categories[0]
        for hour in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("Open AI", "company"), ("Open  AI", "company")],
            )
        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_OPEN_AI_KEY],
        )
        # 両表記は同一名寄せキー → 自己ペア → related に自分自身が出ない。
        assert _OPEN_AI_KEY not in result


class TestMatchKeyExprGuard:
    """``_match_key_expr`` は固定 alias のみ許可し未知 alias を raise で拒否する。

    SQL fragment へ補間される唯一の動的部分なので、injection 経路を構造的に封じる
    (assert ではなく raise で ``-O`` 実行時も剥がれないことを固定)。
    """

    @pytest.mark.parametrize("alias", ["m", "m1", "m2"])
    def test_allows_fixed_aliases(self, alias: str) -> None:
        assert f"{alias}->>'surface'" in _match_key_expr(alias)

    @pytest.mark.parametrize("alias", ["x", "m3", "m; DROP TABLE t", ""])
    def test_rejects_unknown_alias(self, alias: str) -> None:
        with pytest.raises(ValueError, match="alias must be one of"):
            _match_key_expr(alias)


class TestMatchKeyParity:
    """SQL 名寄せ式 ``_match_key_expr`` と Python ``MentionName.match_key`` の一致。

    契約: DB に保存されうる surface (= ``normalize_text`` 出力。NFKC 済 + 内部空白は
    ASCII space/tab/newline のみで ``\\r\\f\\v``・全角を含まない) について
    ``_match_key_expr(surface) == MentionName(surface).match_key``。読取 SQL と書込側
    VO が同一の名寄せキーを生むことを 2 実装の突合で保証する (片方の再実装ではない)。

    casing は両式とも lower 化で畳むため、parity は Postgres ``lower()`` と Python
    ``str.lower()`` が一致する文字に限って成立する。ASCII と一般的なアクセント付き
    Latin (``CAFÉ`` → ``café``、``Société`` → ``société``) は両 engine 一致する。
    Turkish dotted-İ (U+0130) や Greek 語末 sigma は両 engine の lower が分岐する
    既知境界で契約外 (該当 mention は enrich が silent に空になりうる)。実データの
    エンティティ名は ASCII / Latin が支配的なため、この境界は許容する。
    """

    @pytest.mark.parametrize(
        "surface",
        [
            "OpenAI",
            "Open AI",
            "Open  AI",
            "Open\tAI",
            "Open\nAI",
            "  padded  ",
            "NVIDIA Corp",
            "GPT-5",
            "Mixed   \t\n  spaces",
            "UPPER CASE",
            "ALL  CAPS\tCO",
            # アクセント付き Latin は PG lower と str.lower が一致し契約内。
            "CAFÉ",
            "Société",
        ],
    )
    @pytest.mark.asyncio
    async def test_sql_expr_matches_mention_name_match_key(
        self,
        db_session: AsyncSession,
        surface: str,
    ) -> None:
        expected = MentionName(surface).match_key
        # production の SQL 式そのものを literal surface 1 件に適用して評価する
        # (式を再実装せず、_match_key_expr の出力を直接埋め込む)。raw text() では
        # bind の型推論が効かないため CAST で jsonb を明示する。固定リテラル式のみ補間
        # し値は bindparams で渡すため injection 経路はない (S608/text 誤検知)。
        # nosemgrep
        stmt = text(
            f"SELECT {_match_key_expr('m')} AS k "  # noqa: S608 — 固定リテラル式のみ補間
            "FROM (SELECT CAST(:payload AS jsonb) AS m) AS sub"
        ).bindparams(bindparam("payload", json.dumps({"surface": surface})))
        actual = (await db_session.execute(stmt)).scalar_one()
        assert actual == expected


# #8 不正 mention データで window 全体を落とさない (related + ranking 両方を防御)


class TestInvalidMentionDataDoesNotCrashWindow:
    """enum 外 type / 空 surface の legacy・drift 行が混ざっても、当該 1 件のみ skip し

    ranking / related の組み立てが ValidationError を呼び出し元へ伝播させない
    (伝播すると 1 カテゴリの不正行で全カテゴリ snapshot 生成が落ちる)。
    """

    @pytest.mark.asyncio
    async def test_ranked_skips_invalid_type_keeps_valid(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """enum 外 type の mention は pool から除外し、正常 mention は残す。"""
        cat = sample_categories[0]
        for hour in range(MIN_CURRENT):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("NVIDIA", "company")],
            )
        for hour in range(MIN_CURRENT):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 15, hour=hour),
                mentions=[("BadCo", "startup")],  # enum 外 type (drift 行)
            )
        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert {str(r.name) for r in results} == {"NVIDIA"}

    @pytest.mark.asyncio
    async def test_ranked_skips_empty_surface_keeps_valid(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """空 surface (MentionName 検証で落ちる legacy 行) は pool から除外する。"""
        cat = sample_categories[0]
        for hour in range(MIN_CURRENT):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("NVIDIA", "company")],
            )
        for hour in range(MIN_CURRENT):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 15, hour=hour),
                mentions=[("", "company")],  # 空 surface (legacy 行)
            )
        repo = TrendsRepository(db_session)
        results = await repo.get_ranked_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            previous_start=PREV_START,
        )
        assert {str(r.name) for r in results} == {"NVIDIA"}

    @pytest.mark.asyncio
    async def test_related_skips_invalid_co_mention_keeps_valid(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """enum 外 type の共起相手は related から除外し、正常な相手は残す。"""
        cat = sample_categories[0]
        for hour in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[
                    ("NVIDIA", "company"),
                    ("OpenAI", "company"),
                    ("BadCo", "startup"),  # enum 外 type の共起相手
                ],
            )
        repo = TrendsRepository(db_session)
        result = await repo.get_related_mentions(
            category_id=cat.id,
            current_start=WEEK_START,
            current_end=WEEK_END,
            mention_keys=[_NVIDIA_KEY],
        )
        related = result.get(_NVIDIA_KEY, ())
        assert {str(r.name) for r in related} == {"OpenAI"}

    @pytest.mark.asyncio
    async def test_ranked_skip_emits_low_cardinality_warning(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """ranking の skip は failure-visibility の warning を出す (生値は焼かない)。

        skip を silent に握り潰さないことが #8 の眼目なので、warning の発火・低
        cardinality field (category_id / type_known / error_fields) を所有テストとして
        固定する。生 surface / type (PII・高 cardinality) は焼かないことも合わせて縛る。
        """
        cat = sample_categories[0]
        for hour in range(MIN_CURRENT):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 15, hour=hour),
                mentions=[("BadCo", "startup")],  # enum 外 type (drift 行)
            )
        repo = TrendsRepository(db_session)
        with capture_logs() as logs:
            await repo.get_ranked_mentions(
                category_id=cat.id,
                current_start=WEEK_START,
                current_end=WEEK_END,
                previous_start=PREV_START,
            )
        skips = [
            e for e in logs if e["event"] == "trend_ranked_mention_skipped_invalid"
        ]
        assert len(skips) == 1
        event = skips[0]
        assert event["log_level"] == "warning"
        assert event["category_id"] == cat.id
        assert event["type_known"] is False  # "startup" は MentionType 外
        assert "type" in event["error_fields"]
        # 生値 (PII / 高 cardinality) を log に焼かない
        assert "BadCo" not in event.values()
        assert "startup" not in event.values()

    @pytest.mark.asyncio
    async def test_related_skip_emits_low_cardinality_warning(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """related の skip も同じ failure-visibility の warning を出す。"""
        cat = sample_categories[0]
        for hour in range(2):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[
                    ("NVIDIA", "company"),
                    ("OpenAI", "company"),
                    ("BadCo", "startup"),  # enum 外 type の共起相手
                ],
            )
        repo = TrendsRepository(db_session)
        with capture_logs() as logs:
            await repo.get_related_mentions(
                category_id=cat.id,
                current_start=WEEK_START,
                current_end=WEEK_END,
                mention_keys=[_NVIDIA_KEY],
            )
        skips = [
            e for e in logs if e["event"] == "trend_related_mention_skipped_invalid"
        ]
        assert len(skips) == 1
        event = skips[0]
        assert event["log_level"] == "warning"
        assert event["category_id"] == cat.id
        assert event["type_known"] is False
        assert "BadCo" not in event.values()
        assert "startup" not in event.values()
