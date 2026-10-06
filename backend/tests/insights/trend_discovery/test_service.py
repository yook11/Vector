"""週の準備からトレンドの集計・保存までを実DBで確認する。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insights.trend_discovery.domain.trend import (
    MIN_CANDIDATE_COUNT,
    TOP_N_PER_RANKING,
    TrendWeeks,
)
from app.insights.trend_discovery.repository import (
    SnapshotRepository,
    SnapshotSaveResult,
    SnapshotSaveStatus,
    TrendsRepository,
)
from app.insights.trend_discovery.schemas import Trends
from app.insights.trend_discovery.service import (
    SkippedAlreadyGenerated,
    SkippedNoTargetArticles,
    TrendDiscoveryCompleted,
    TrendDiscoveryConflict,
    TrendDiscoveryService,
)
from app.models.category import Category

from .conftest import SeedAnalysis

JST = ZoneInfo("Asia/Tokyo")
# 週は 2026-04-13 から 2026-04-19、前週は 2026-04-06 から 2026-04-12。
SNAPSHOT_DATE = date(2026, 4, 20)


def _weeks() -> TrendWeeks:
    return TrendWeeks(snapshot_date=SNAPSHOT_DATE)


def _section(bundle: dict, category: Category) -> dict:
    return next(
        c for c in bundle["categoryTrends"] if c["category"]["slug"] == category.slug
    )


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
        """週に公開された記事が 0 件なら category 集計も snapshot 保存も行わない。"""
        service = TrendDiscoveryService(session_factory)
        with patch.object(
            TrendsRepository,
            "get_categories",
            new=AsyncMock(side_effect=AssertionError("category fetch not expected")),
        ):
            result = await service.execute(_weeks())

        assert isinstance(result, SkippedNoTargetArticles)
        assert result.snapshot_date == SNAPSHOT_DATE
        assert result.analyzed_article_count == 0
        assert result.completed_category_count is None

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_snapshot_date(SNAPSHOT_DATE)
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
        result = await service.execute(_weeks())

        assert isinstance(result, TrendDiscoveryCompleted)
        assert result.snapshot_date == SNAPSHOT_DATE
        assert result.analyzed_article_count == 10
        assert result.completed_category_count == len(sample_categories)

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_snapshot_date(SNAPSHOT_DATE)
        assert snapshot is not None
        assert (snapshot.window_end, snapshot.source_analysis_count) == (
            SNAPSHOT_DATE,
            10,
        )
        assert snapshot.bundle["analyzedArticleCount"] == 10

    @pytest.mark.asyncio
    async def test_bundle_contains_all_categories(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """全カテゴリを 1 つずつ含む (名前が無くても空のカテゴリとして残る)。"""
        for i in range(10):
            await seed_analysis(
                category_id=sample_categories[0].id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_weeks())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_snapshot_date(SNAPSHOT_DATE)
        assert snapshot is not None
        assert sorted(
            c["category"]["slug"] for c in snapshot.bundle["categoryTrends"]
        ) == sorted(c.slug for c in sample_categories)

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
        for hour in range(MIN_CANDIDATE_COUNT):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                mentions=[("NVIDIA", "company")],
            )
        for hour in range(MIN_CANDIDATE_COUNT):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 15, hour=hour),
                mentions=[("BadCo", "startup")],  # enum 外 type (drift 行)
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        result = await service.execute(_weeks())

        assert isinstance(result, TrendDiscoveryCompleted)
        assert result.completed_category_count == len(sample_categories)

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_snapshot_date(SNAPSHOT_DATE)
        assert snapshot is not None
        names = {m["name"] for m in _section(snapshot.bundle, cat)["mentionTrends"]}
        assert names == {"NVIDIA"}

    @pytest.mark.asyncio
    async def test_saves_only_names_ranked_within_top_n(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """どちらの順位も ``TOP_N_PER_RANKING`` を超える名前は保存しない。

        entity_i は週 (10-i) 件・前週 2 件で、記事数も伸び率も i の順に並ぶ。
        TOP_N_PER_RANKING + 1 番目の entity だけがどちらの上位にも入らない。
        """
        cat = sample_categories[0]
        for i in range(TOP_N_PER_RANKING + 1):
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
        await service.execute(_weeks())

        snapshot = await SnapshotRepository(db_session).find_by_snapshot_date(
            SNAPSHOT_DATE
        )
        assert snapshot is not None
        section = _section(snapshot.bundle, cat)
        assert [
            (m["name"], m["articleVolume"]["rank"], m["growth"]["rank"])
            for m in section["mentionTrends"]
        ] == [
            ("entity_00", 1, 1),
            ("entity_01", 2, 2),
            ("entity_02", 3, 3),
            ("entity_03", 4, 4),
            ("entity_04", 5, 5),
        ]

    @pytest.mark.asyncio
    async def test_bundle_is_camel_case_api_payload(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """保存済み bundle は camelCase API payload であり、Trends として
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
        await service.execute(_weeks())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_snapshot_date(SNAPSHOT_DATE)
        assert snapshot is not None
        bundle = snapshot.bundle

        round_tripped = Trends.model_validate(bundle).model_dump(
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

        previous_week / category_trends / article_volume がキーとして現れれば
        snake_case dump への退行を意味する。
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
        await service.execute(_weeks())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_snapshot_date(SNAPSHOT_DATE)
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
        assert {"previous_week", "category_trends", "article_volume"}.isdisjoint(
            all_keys
        )

    @pytest.mark.asyncio
    async def test_bundle_weeks_end_on_the_last_day(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """週はスナップショットの日付の前日まで、前週はその直前の7日間。"""
        cat = sample_categories[0]
        for i in range(10):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=i),
                mentions=[("NVIDIA", "company")],
            )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_weeks())

        snapshot = await SnapshotRepository(db_session).find_by_snapshot_date(
            SNAPSHOT_DATE
        )
        assert snapshot is not None
        assert (snapshot.bundle["week"], snapshot.bundle["previousWeek"]) == (
            {"start": "2026-04-13", "end": "2026-04-19"},
            {"start": "2026-04-06", "end": "2026-04-12"},
        )

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
        await service.execute(_weeks())

        repo = SnapshotRepository(db_session)
        snapshot = await repo.find_by_snapshot_date(SNAPSHOT_DATE)
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
    async def test_article_volume_rank_follows_week_counts(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """記事数の順位は週の記事数の多い順で、名前はその順に並ぶ。"""
        cat = sample_categories[0]
        # entity_a: 10件, entity_b: 7件, entity_c: 5件 (下限 MIN_CANDIDATE_COUNT)
        for name, type_, count in (
            ("entity_a", "company", 10),
            ("entity_b", "technology", 7),
            ("entity_c", "company", 5),
        ):
            for i in range(count):
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 14, hour=i),
                    mentions=[(name, type_)],
                )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_weeks())

        snapshot = await SnapshotRepository(db_session).find_by_snapshot_date(
            SNAPSHOT_DATE
        )
        assert snapshot is not None
        assert [
            (m["name"], m["articleVolume"]["count"], m["articleVolume"]["rank"])
            for m in _section(snapshot.bundle, cat)["mentionTrends"]
        ] == [("entity_a", 10, 1), ("entity_b", 7, 2), ("entity_c", 5, 3)]

    @pytest.mark.asyncio
    async def test_growth_rank_follows_growth_rates(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """伸び率の順位は伸び率の高い順。"""
        cat = sample_categories[0]
        # entity_burst: 前週0, 週12 → 12/2 = 6.0 (急増)
        # entity_growth: 前週3, 週10 → 7/3 ≈ 2.33
        # entity_steady: 前週4, 週8 → 4/4 = 1.0
        for name, type_, week_count, previous_week_count in (
            ("entity_burst", "company", 12, 0),
            ("entity_growth", "technology", 10, 3),
            ("entity_steady", "company", 8, 4),
        ):
            for i in range(week_count):
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 14, hour=i),
                    mentions=[(name, type_)],
                )
            for i in range(previous_week_count):
                await seed_analysis(
                    category_id=cat.id,
                    analyzed_at=_jst(2026, 4, 7, hour=i),
                    mentions=[(name, type_)],
                )
        await db_session.commit()

        service = TrendDiscoveryService(session_factory)
        await service.execute(_weeks())

        snapshot = await SnapshotRepository(db_session).find_by_snapshot_date(
            SNAPSHOT_DATE
        )
        assert snapshot is not None
        assert {
            m["name"]: m["growth"]["rank"]
            for m in _section(snapshot.bundle, cat)["mentionTrends"]
        } == {"entity_burst": 1, "entity_growth": 2, "entity_steady": 3}

    @pytest.mark.asyncio
    async def test_name_without_growth_basis_has_no_growth_rank(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """前週1件・週7件の名前は記事数の順位だけを持ち、伸び率の順位は持たない。

        記事数の順位は候補全体、伸び率の順位は伸び率で並べる条件 (前週2件以上 or
        週10件以上) を満たす名前だけが母集団になる。
        """
        cat = sample_categories[0]
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

        category_trends = await TrendDiscoveryService._build_category_trends(
            TrendsRepository(db_session), category=cat, weeks=_weeks()
        )

        (edge,) = category_trends.mention_trends
        assert (edge.article_volume.rank, edge.growth.rank) == (1, None)

    @pytest.mark.asyncio
    async def test_attaches_key_points_and_co_mentions_to_published_names(
        self,
        db_session: AsyncSession,
        sample_categories: list[Category],
        seed_analysis: SeedAnalysis,
    ) -> None:
        """公開する名前に、最新3記事の要点と一緒に語られた名前を付ける。"""
        cat = sample_categories[0]
        for hour in range(12):
            await seed_analysis(
                category_id=cat.id,
                analyzed_at=_jst(2026, 4, 14, hour=hour),
                content=f"NVIDIA point {hour}",
                mentions=[("NVIDIA", "company"), ("OpenAI", "company")],
                embedding=[1.0, 0.0],
            )
        await db_session.commit()

        category_trends = await TrendDiscoveryService._build_category_trends(
            TrendsRepository(db_session), category=cat, weeks=_weeks()
        )

        nvidia = next(
            t for t in category_trends.mention_trends if str(t.name) == "NVIDIA"
        )
        # 同じベクトルの記事でも最新3記事の要点が付く。
        assert nvidia.key_points == (
            "NVIDIA point 11",
            "NVIDIA point 10",
            "NVIDIA point 9",
        )
        assert {str(c.name) for c in nvidia.mentioned_with} == {"OpenAI"}


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
                "find_by_snapshot_date",
                new=AsyncMock(return_value=None),
            ) as find_by_snapshot_date,
        ):
            result = await service.execute(_weeks())

        assert isinstance(result, TrendDiscoveryConflict)
        assert result.snapshot_date == SNAPSHOT_DATE
        assert result.analyzed_article_count == 1
        assert result.completed_category_count == len(sample_categories)
        find_by_snapshot_date.assert_not_awaited()


class TestOutcomeTypes:
    def test_completed_is_frozen(self) -> None:
        outcome = TrendDiscoveryCompleted(
            snapshot_date=date(2026, 4, 20),
            analyzed_article_count=10,
            completed_category_count=3,
        )
        with pytest.raises(AttributeError):
            outcome.analyzed_article_count = 99  # type: ignore[misc]

    def test_skipped_no_target_articles_is_frozen(self) -> None:
        outcome = SkippedNoTargetArticles(snapshot_date=date(2026, 4, 20))
        with pytest.raises(AttributeError):
            outcome.analyzed_article_count = 99  # type: ignore[misc]

    def test_conflict_is_frozen(self) -> None:
        outcome = TrendDiscoveryConflict(
            snapshot_date=date(2026, 4, 20),
            analyzed_article_count=10,
            completed_category_count=3,
        )
        with pytest.raises(AttributeError):
            outcome.completed_category_count = 99  # type: ignore[misc]


@pytest.mark.asyncio
async def test_skips_when_only_recent_analyses_of_old_publications_exist(
    db_session, session_factory, sample_categories, seed_analysis
):
    """週に分析した記事があっても、週の外に公開されたものなら対象なしとして終了する。"""
    await seed_analysis(
        category_id=sample_categories[0].id,
        published_at=_jst(2026, 4, 1),
        analyzed_at=_jst(2026, 4, 14),
        mentions=[("NVIDIA", "company")],
    )
    await db_session.commit()
    result = await TrendDiscoveryService(session_factory).execute(_weeks())
    assert isinstance(result, SkippedNoTargetArticles)
    assert (
        await SnapshotRepository(db_session).find_by_snapshot_date(SNAPSHOT_DATE)
        is None
    )


@pytest.fixture
def create_dependencies(monkeypatch):
    from app.insights.trend_discovery import service as service_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 19, 15, 5, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(service_module, "datetime", FixedDatetime)
    audit = AsyncMock()
    monkeypatch.setattr(
        service_module, "append_trend_discovery_run_event_best_effort", audit
    )
    return TrendDiscoveryService(MagicMock()), AsyncMock(), audit


class TestCreate:
    @pytest.mark.asyncio
    async def test_selects_the_latest_completed_week(self, create_dependencies):
        """現在時刻からドメインが定義した週を実行へ渡す。"""
        service, notifier, _ = create_dependencies
        service.execute = AsyncMock(
            return_value=SkippedAlreadyGenerated(snapshot_date=SNAPSHOT_DATE)
        )
        await service.create(notifier)
        service.execute.assert_awaited_once_with(_weeks())

    @pytest.mark.asyncio
    async def test_records_completion_after_successful_generation(
        self, create_dependencies
    ):
        """成功した週と集計件数を、監査の項目名 (window_*) のまま渡す。"""
        from app.audit.domain.event import EventType
        from app.audit.stages.trend_discovery import TrendDiscoveryOutcomeCode

        service, notifier, audit = create_dependencies
        service.execute = AsyncMock(
            return_value=TrendDiscoveryCompleted(SNAPSHOT_DATE, 42, 3)
        )
        await service.create(notifier)
        audit.assert_awaited_once_with(
            service._session_factory,
            event_type=EventType.SUCCEEDED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_COMPLETED,
            window_start=date(2026, 4, 13),
            window_end=SNAPSHOT_DATE,
            source_analysis_count=42,
            completed_category_count=3,
        )

    @pytest.mark.asyncio
    async def test_notifies_after_successful_generation(self, create_dependencies):
        """保存が成功した場合だけトレンドの表示更新を通知する。"""
        service, notifier, _ = create_dependencies
        service.execute = AsyncMock(
            return_value=TrendDiscoveryCompleted(SNAPSHOT_DATE, 42, 3)
        )
        await service.create(notifier)
        notifier.notify.assert_awaited_once_with(tags=("trends",))

    @pytest.mark.asyncio
    async def test_existing_period_does_not_audit_or_notify(self, create_dependencies):
        """生成済みで終了した場合は成功監査や表示更新を追加しない。"""
        service, notifier, audit = create_dependencies
        service.execute = AsyncMock(return_value=SkippedAlreadyGenerated(SNAPSHOT_DATE))
        await service.create(notifier)
        audit.assert_not_awaited()
        notifier.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_articles_does_not_audit_or_notify(self, create_dependencies):
        """対象0件で終了した場合は成功監査や表示更新を追加しない。"""
        service, notifier, audit = create_dependencies
        service.execute = AsyncMock(return_value=SkippedNoTargetArticles(SNAPSHOT_DATE))
        await service.create(notifier)
        audit.assert_not_awaited()
        notifier.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_conflict_does_not_audit_or_notify(self, create_dependencies):
        """保存競合の敗者は成功監査や表示更新を追加しない。"""
        service, notifier, audit = create_dependencies
        service.execute = AsyncMock(
            return_value=TrendDiscoveryConflict(SNAPSHOT_DATE, 42, 3)
        )
        await service.create(notifier)
        audit.assert_not_awaited()
        notifier.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execution_failure_is_audited_and_reraised(self, create_dependencies):
        """準備・集計・保存を含むexecuteの失敗を監査して再送出する。"""
        from app.audit.domain.event import EventType
        from app.audit.stages.trend_discovery import TrendDiscoveryOutcomeCode

        service, notifier, audit = create_dependencies
        error = RuntimeError("execution failed")
        service.execute = AsyncMock(side_effect=error)
        with pytest.raises(RuntimeError, match="execution failed"):
            await service.create(notifier)
        audit.assert_awaited_once_with(
            service._session_factory,
            event_type=EventType.FAILED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_FAILED,
            window_start=date(2026, 4, 13),
            window_end=SNAPSHOT_DATE,
            exc=error,
        )
        notifier.notify.assert_not_awaited()
