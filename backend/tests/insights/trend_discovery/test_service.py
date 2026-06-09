"""TrendDiscoveryService.execute の挙動テスト。

検証する観点:
- execute(ready, force=False) 正常系: TrendDiscoveryCompleted を返し
  snapshot を 1 行保存
- 集計対象記事 0 件: snapshot を保存せず SkippedNoTargetArticles を返す
- execute(ready, force=True) 既存上書き: TrendDiscoveryCompleted を返し
  source_analysis_count を反映 (`generated_at` も更新)
- bundle 内容: camelCase payload として保存 (契約適合)。全カテゴリ 1 セクション
  ずつ含み、出現回数 / 伸び率の 2 ランキングがそれぞれの母集団で確定する
- source_analysis_count: window 内の analysis 件数 (全カテゴリ合算)
- 各ランキングは TOP_N_PER_RANKING 件で truncate
- 上位 mention に key_point / related mention の文脈が付き、両ランキングに載る
  mention は同じ enrich 済みインスタンスを共有する
- race 敗北 (force=False で同時 INSERT 競合): 読み戻しせず
  TrendDiscoveryConflict を返す

既存 snapshot skip は ``ReadyForTrendDiscovery.try_advance_from`` 側に移管されている。
一方、集計対象記事 0 件は Service が ``SkippedNoTargetArticles`` として返す。

集計窓は rolling 7d で半開区間 ``[window_end - 7d, window_end)`` を取る。
window_end = 2026-04-20 のとき、window = [2026-04-13 0:00 JST, 2026-04-20 0:00 JST)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insights.trend_discovery.application.service import (
    SkippedNoTargetArticles,
    TrendDiscoveryCompleted,
    TrendDiscoveryConflict,
    TrendDiscoveryService,
)
from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery
from app.insights.trend_discovery.domain.trend import MIN_CURRENT, TOP_N_PER_RANKING
from app.insights.trend_discovery.repository.snapshots import (
    SnapshotRepository,
    SnapshotSaveResult,
    SnapshotSaveStatus,
)
from app.insights.trend_discovery.repository.trends import TrendsRepository
from app.insights.trend_discovery.schemas.trends import TrendsResponse
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
        assert snapshot.bundle["windowEnd"] == WINDOW_END.isoformat()

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
        category_trends = snapshot.bundle["categoryTrends"]
        assert len(category_trends) == len(sample_categories)
        category_ids = {c["categoryId"] for c in category_trends}
        assert category_ids == {c.id for c in sample_categories}

    @pytest.mark.asyncio
    async def test_generates_snapshot_when_category_has_invalid_mentions(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """不正 mention (enum 外 type) が 1 カテゴリに混ざっても execute は落ちず、

        正常 mention は残り不正行のみ skip される (1 行で全カテゴリ snapshot 生成を
        巻き込まない: #8)。
        """
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
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        result = await service.execute(_ready())

        assert isinstance(result, TrendDiscoveryCompleted)
        assert result.completed_category_count == len(sample_categories)

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        section = next(
            c for c in snapshot.bundle["categoryTrends"] if c["categoryId"] == cat.id
        )
        names = {m["name"] for m in section["mostMentioned"]}
        assert names == {"NVIDIA"}

    @pytest.mark.asyncio
    async def test_caps_each_ranking_at_top_n(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """出現回数 / 伸び率の各ランキングは ``TOP_N_PER_RANKING`` 件で truncate。

        floor 通過 mention は 1 カテゴリで多数に膨らむため、各ランキング上位 N 件で
        切ることで JSONB 肥大化と UI ノイズを構造的に防ぐ。entity_00 が最多出現
        (= 上位)、entity が小さいほど件数が多くなるよう仕込む。
        """
        cat = sample_categories[0]
        # TOP_N_PER_RANKING + 1 件を母集団に投入し、上位 TOP_N_PER_RANKING 件に
        # truncate されることを確認する。entity_i は current=(10-i) 件・previous=2 件。
        # previous>=2 で hot ゲートを通すため両ランキングに投入数が母集団入りし、
        # 上位 TOP_N_PER_RANKING 件で truncate される。
        _over = TOP_N_PER_RANKING + 1
        _dropped_name = f"entity_{TOP_N_PER_RANKING:02d}"  # 末尾の除外対象
        for i in range(_over):
            for hour in range(10 - i):
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 14, hour=hour),
                    mentions=[(f"entity_{i:02d}", "company")],
                )
            for hour in range(2):
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 7, hour=hour),
                    mentions=[(f"entity_{i:02d}", "company")],
                )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_ready())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        category_trends = next(
            c for c in snapshot.bundle["categoryTrends"] if c["categoryId"] == cat.id
        )
        assert len(category_trends["mostMentioned"]) == TOP_N_PER_RANKING
        assert len(category_trends["fastestGrowing"]) == TOP_N_PER_RANKING
        appearance_names = [m["name"] for m in category_trends["mostMentioned"]]
        assert appearance_names[0] == "entity_00"  # 最多出現が先頭
        assert _dropped_name not in appearance_names  # TOP_N 番目は上位から漏れる

    @pytest.mark.asyncio
    async def test_bundle_is_camel_case_api_payload(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """保存済み bundle は camelCase API payload であり、TrendsResponse として
        再検証→再シリアライズしても同値になる (verbatim 保存の契約適合)。

        ``populate_by_name=True`` で snake_case でも validate が通るため、
        exact equality で camelCase が保存されていることを確認する。
        """
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

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        bundle = snapshot.bundle

        ta = TypeAdapter(TrendsResponse)
        round_tripped = ta.validate_python(bundle).model_dump(
            mode="json", by_alias=True
        )
        assert round_tripped == bundle

    @pytest.mark.asyncio
    async def test_bundle_contains_no_snake_case_keys(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """保存済み bundle に snake_case キーが存在しない (camelCase のみ)。

        window_start / category_trends / hotness_score がキーとして現れれば
        旧 snake_case dump への退行を意味する。
        """
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("NVIDIA", "company"), ("OpenAI", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_ready())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None

        def _all_keys(obj: object) -> set[str]:
            keys: set[str] = set()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    keys.add(k)
                    keys |= _all_keys(v)
            elif isinstance(obj, list):
                for item in obj:
                    keys |= _all_keys(item)
            return keys

        all_keys = _all_keys(snapshot.bundle)
        # snake_case 退行検出: これらのキーが現れれば旧 dump 形式への回帰
        assert "window_start" not in all_keys
        assert "category_trends" not in all_keys
        assert "hotness_score" not in all_keys

    @pytest.mark.asyncio
    async def test_bundle_window_start_is_window_end_minus_7_days(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """bundle["windowStart"] == bundle["windowEnd"] - 7d (API contract)。"""
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

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        from datetime import date as _date

        window_end = _date.fromisoformat(snapshot.bundle["windowEnd"])
        window_start = _date.fromisoformat(snapshot.bundle["windowStart"])
        assert window_start == window_end - timedelta(days=7)

    @pytest.mark.asyncio
    async def test_bundle_generated_at_equals_db_column(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """bundle["generatedAt"] と TrendsSnapshot.generated_at 列が同値。

        service は generated_at を1つ確定し payload と DB 列の双方へ同値を入れる
        (server_default を持たない)。両者のズレはその保証の破れを意味する。
        """
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

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None

        # payload の generatedAt は ISO 8601 文字列 (timezone-aware)
        payload_generated_at = datetime.fromisoformat(snapshot.bundle["generatedAt"])
        db_generated_at = snapshot.generated_at
        # タイムゾーン情報があれば揃えて比較、なければ UTC と仮定
        if payload_generated_at.tzinfo is None:
            payload_generated_at = payload_generated_at.replace(tzinfo=UTC)
        if db_generated_at.tzinfo is None:
            db_generated_at = db_generated_at.replace(tzinfo=UTC)
        assert payload_generated_at == db_generated_at

    @pytest.mark.asyncio
    async def test_most_mentioned_ordered_by_appearance_count_desc(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """mostMentioned は appearanceCount 降順で並ぶ。"""
        cat = sample_categories[0]
        # entity_a: 10件, entity_b: 7件, entity_c: 5件 (floor=MIN_CURRENT)
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("entity_a", "company")],
            )
        for i in range(7):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("entity_b", "technology")],
            )
        for i in range(5):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("entity_c", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_ready())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        ct = next(
            c for c in snapshot.bundle["categoryTrends"] if c["categoryId"] == cat.id
        )
        counts = [m["appearanceCount"] for m in ct["mostMentioned"]]
        assert counts == sorted(counts, reverse=True)

    @pytest.mark.asyncio
    async def test_fastest_growing_ordered_by_growth_rate_desc(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """fastestGrowing は growthRate 降順で並ぶ。"""
        cat = sample_categories[0]
        # entity_burst: previous=0, current=12 → hotness = 12/2 = 6.0 (burst)
        # entity_growth: previous=3, current=10 → hotness = 7/3 ≈ 2.33
        # entity_steady: previous=4, current=8 → hotness = 4/4 = 1.0
        for i in range(12):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i % 12),
                mentions=[("entity_burst", "company")],
            )
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i % 10),
                mentions=[("entity_growth", "technology")],
            )
        for i in range(3):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 7, hour=i),
                mentions=[("entity_growth", "technology")],
            )
        for i in range(8):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i % 8),
                mentions=[("entity_steady", "company")],
            )
        for i in range(4):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 7, hour=i),
                mentions=[("entity_steady", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_ready())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        ct = next(
            c for c in snapshot.bundle["categoryTrends"] if c["categoryId"] == cat.id
        )
        rates = [m["growthRate"] for m in ct["fastestGrowing"]]
        assert rates == sorted(rates, reverse=True)

    @pytest.mark.asyncio
    async def test_mention_in_both_rankings_has_consistent_shape(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """両ランキングに登場する mention は同じ shape を持つ。"""
        cat = sample_categories[0]
        # NVIDIA: current=12, previous=0 → 出現多+burst で両方上位
        for i in range(12):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i % 12),
                mentions=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_ready())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_window_end(WINDOW_END)
        assert snapshot is not None
        ct = next(
            c for c in snapshot.bundle["categoryTrends"] if c["categoryId"] == cat.id
        )

        mm_nvidia = next(
            (m for m in ct["mostMentioned"] if m["name"] == "NVIDIA"), None
        )
        fg_nvidia = next(
            (m for m in ct["fastestGrowing"] if m["name"] == "NVIDIA"), None
        )
        assert mm_nvidia is not None and fg_nvidia is not None
        # 同じ mention は同じ appearanceCount / type を持つ
        assert mm_nvidia["appearanceCount"] == fg_nvidia["appearanceCount"]
        assert mm_nvidia["type"] == fg_nvidia["type"]

    @pytest.mark.asyncio
    async def test_floor_passing_non_hot_appears_in_appearance_not_growth(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """floor は超えるが hot ゲート外の高出現 mention は出現回数にだけ載る。

        2 ランキングの母集団が異なる (出現回数=floor のみ / 伸び率=floor+hot) こと
        の回帰。current>=5 だが previous<2 かつ current<burst の mention は
        most_mentioned に出るが fastest_growing には出ない。
        """
        cat = sample_categories[0]
        # current 7 / previous 1 → floor 通過・hot ゲート外。
        for hour in range(7):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("Edge", "technology")],
            )
        await seed_analysis(
            category_id=cat.id,
            analyzed_at=_jst(2026, 4, 7, hour=9),
            mentions=[("Edge", "technology")],
        )
        await db_session.commit()

        repo = TrendsRepository(db_session)
        category_trends = await TrendDiscoveryService._build_category_trends(
            repo,
            category=cat,
            current_start=_jst(2026, 4, 13, hour=0),
            current_end=_jst(2026, 4, 20, hour=0),
            previous_start=_jst(2026, 4, 6, hour=0),
        )

        appearance_names = {str(m.name) for m in category_trends.most_mentioned}
        growth_names = {str(m.name) for m in category_trends.fastest_growing}
        assert "Edge" in appearance_names
        assert "Edge" not in growth_names

    @pytest.mark.asyncio
    async def test_enriches_shared_mention_once_across_rankings(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """両ランキングに載る mention は同一 enrich 済みインスタンスを共有する。"""
        cat = sample_categories[0]
        # current 多数 / previous 0 → 出現回数・伸び率の両方で上位に来る burst。
        for hour in range(12):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                content=f"NVIDIA point {hour}",
                mentions=[("NVIDIA", "company"), ("OpenAI", "company")],
                embedding=[1.0, 0.0],  # 同一トピック → key_point は 1 本に畳まれる
            )
        await db_session.commit()

        repo = TrendsRepository(db_session)
        category_trends = await TrendDiscoveryService._build_category_trends(
            repo,
            category=cat,
            current_start=_jst(2026, 4, 13, hour=0),
            current_end=_jst(2026, 4, 20, hour=0),
            previous_start=_jst(2026, 4, 6, hour=0),
        )

        appearance = next(
            m for m in category_trends.most_mentioned if str(m.name) == "NVIDIA"
        )
        growth = next(
            m for m in category_trends.fastest_growing if str(m.name) == "NVIDIA"
        )
        # 同一インスタンス共有 (二重 enrich なし)。
        assert appearance is growth
        # 文脈が付いている (related に OpenAI、key_point は記事 dedup で 1 本)。
        assert len(appearance.key_points) == 1
        assert {str(r.name) for r in appearance.related_mentions} == {"OpenAI"}


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
