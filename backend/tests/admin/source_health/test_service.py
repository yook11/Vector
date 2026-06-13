"""SourceHealthService の集計ロジック (integration)。

観測時刻 ``observed_at`` を固定注入し、source 別 health 指標の不変条件を確認する。
期待値は seed 入力から導出し、除外されるべきデータを必ず混ぜて非空虚にする。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.source_health.repository import SourceHealthRepository
from app.admin.source_health.schemas import SourceHealthItem, SourceHealthResponse
from app.admin.source_health.service import SourceHealthService
from app.audit.domain.event import EventType, Stage
from app.models.incomplete_article import IncompleteArticle
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent

# 観測基準時刻 (固定注入で窓計算を決定的にする)。
_OBSERVED_AT = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)

_ARTICLE_CREATED = "article_created"
_ARTICLE_COMPLETED = "article_completed"
_INCOMPLETE_ARTICLE_CREATED = "incomplete_article_created"


async def _health(
    session: AsyncSession, *, observed_at: datetime, window_hours: int = 24
) -> SourceHealthResponse:
    service = SourceHealthService(SourceHealthRepository(session))
    return await service.get_health(window_hours=window_hours, observed_at=observed_at)


def _item(result: SourceHealthResponse, source_id: int) -> SourceHealthItem:
    return next(i for i in result.items if i.source_id == source_id)


async def _make_source(
    session: AsyncSession,
    name: str,
    *,
    slug: str,
    is_active: bool = True,
    source_type: SourceType = SourceType.RSS,
) -> NewsSource:
    source = NewsSource(
        name=name,
        source_type=source_type,
        site_url=f"https://{slug}.example.com",
        endpoint_url=f"https://{slug}.example.com/feed",
        is_active=is_active,
    )
    session.add(source)
    await session.flush()
    await session.refresh(source)
    return source


def _ev(
    source_id: int | None,
    *,
    stage: Stage,
    event_type: EventType,
    outcome_code: str,
    occurred_at: datetime,
) -> PipelineEvent:
    return PipelineEvent(
        source_id=source_id,
        stage=stage,
        event_type=event_type,
        outcome_code=outcome_code,
        occurred_at=occurred_at,
    )


def _incomplete(
    source: NewsSource,
    *,
    status: str,
    created_at: datetime,
    url: str,
    ready_at: datetime | None = None,
    leased_until: datetime | None = None,
) -> IncompleteArticle:
    return IncompleteArticle(
        url=url,
        source_id=source.id,
        source_name=source.name,
        status=status,
        observed_article={},
        created_at=created_at,
        ready_at=ready_at,
        leased_until=leased_until,
    )


async def test_analyzable_sums_article_created_and_completed(
    db_session: AsyncSession,
) -> None:
    """analyzable は article_created と article_completed の合計 (両 stage を足す)。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add_all(
        [
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=2),
            ),
            _ev(
                source.id,
                stage=Stage.COMPLETION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_COMPLETED,
                occurred_at=obs - timedelta(hours=3),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    # 2 (article_created) + 1 (article_completed)。片方だけではない。
    assert item.analyzable_count == 3
    assert item.processed_article_count == 3
    assert item.analyzable_rate == 100.0


async def test_stage_outcome_mismatch_is_not_analyzable(
    db_session: AsyncSession,
) -> None:
    """stage×outcome 入れ違い / incomplete_article_created は analyzable に数えない。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add_all(
        [
            # completion で article_created は analyzable ではない。
            _ev(
                source.id,
                stage=Stage.COMPLETION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            # acquisition で article_completed も analyzable ではない。
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_COMPLETED,
                occurred_at=obs - timedelta(hours=1),
            ),
            # incomplete_article_created は未確定なので除外。
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_INCOMPLETE_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    assert item.analyzable_count == 0
    assert item.processed_article_count == 0
    assert item.analyzable_rate is None


async def test_incomplete_article_created_excluded_from_processed(
    db_session: AsyncSession,
) -> None:
    """incomplete_article_created は processed/analyzable に乗らない。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add_all(
        [
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_INCOMPLETE_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_INCOMPLETE_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    # article_created の 1 件のみ。incomplete 2 件は無視。
    assert item.analyzable_count == 1
    assert item.processed_article_count == 1


async def test_rejected_counts_toward_processed_and_failures(
    db_session: AsyncSession,
) -> None:
    """rejected は processed と failure reasons の両方に反映される。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add_all(
        [
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            _ev(
                source.id,
                stage=Stage.COMPLETION,
                event_type=EventType.REJECTED,
                outcome_code="scrape_not_html",
                occurred_at=obs - timedelta(hours=2),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    assert item.analyzable_count == 1
    # analyzable 1 + rejected 1。
    assert item.processed_article_count == 2
    assert item.analyzable_rate == 50.0
    assert item.failure_reasons[0].outcome_code == "scrape_not_html"
    assert item.failure_reasons[0].count == 1


async def test_failed_excluded_from_processed_but_in_failures(
    db_session: AsyncSession,
) -> None:
    """failed は processed に入らず failure reasons には入る。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add_all(
        [
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            _ev(
                source.id,
                stage=Stage.COMPLETION,
                event_type=EventType.FAILED,
                outcome_code="fetch_timeout",
                occurred_at=obs - timedelta(hours=2),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    assert item.analyzable_count == 1
    # failed は processed に入らない (analyzable 1 のまま)。
    assert item.processed_article_count == 1
    assert item.analyzable_rate == 100.0
    assert [(f.outcome_code, f.count) for f in item.failure_reasons] == [
        ("fetch_timeout", 1)
    ]


async def test_failure_reasons_sorted_count_desc_then_code_asc(
    db_session: AsyncSession,
) -> None:
    """failure reasons は count 降順、同数は outcomeCode 昇順で全件返る。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add_all(
        [
            # code_c: 3 件 (failed)。
            *[
                _ev(
                    source.id,
                    stage=Stage.COMPLETION,
                    event_type=EventType.FAILED,
                    outcome_code="code_c",
                    occurred_at=obs - timedelta(hours=1),
                )
                for _ in range(3)
            ],
            # code_a: 2 件 (rejected)。
            *[
                _ev(
                    source.id,
                    stage=Stage.ACQUISITION,
                    event_type=EventType.REJECTED,
                    outcome_code="code_a",
                    occurred_at=obs - timedelta(hours=1),
                )
                for _ in range(2)
            ],
            # code_b: 2 件 (failed)。code_a と同数 → 昇順で code_a が先。
            *[
                _ev(
                    source.id,
                    stage=Stage.COMPLETION,
                    event_type=EventType.FAILED,
                    outcome_code="code_b",
                    occurred_at=obs - timedelta(hours=1),
                )
                for _ in range(2)
            ],
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    assert [(f.outcome_code, f.count) for f in item.failure_reasons] == [
        ("code_c", 3),
        ("code_a", 2),
        ("code_b", 2),
    ]


async def test_window_start_is_inclusive_lower_bound(
    db_session: AsyncSession,
) -> None:
    """window start ちょうどの event は含み、それより前は除外する。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    window_start = obs - timedelta(hours=24)
    db_session.add_all(
        [
            # window start ちょうど → 含む。
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=window_start,
            ),
            # window start の 1 秒前 → 除外。
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=window_start - timedelta(seconds=1),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    assert item.analyzable_count == 1


async def test_window_hours_widens_event_window(
    db_session: AsyncSession,
) -> None:
    """window_hours を広げると、より古い event が窓に入る。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add(
        _ev(
            source.id,
            stage=Stage.ACQUISITION,
            event_type=EventType.SUCCEEDED,
            outcome_code=_ARTICLE_CREATED,
            occurred_at=obs - timedelta(hours=40),
        )
    )
    await db_session.flush()

    # 24h 窓では窓外。
    item_24 = _item(
        await _health(db_session, observed_at=obs, window_hours=24), source.id
    )
    assert item_24.analyzable_count == 0
    # 48h 窓では窓内。
    item_48 = _item(
        await _health(db_session, observed_at=obs, window_hours=48), source.id
    )
    assert item_48.analyzable_count == 1


async def test_incomplete_count_open_running_only_window_independent(
    db_session: AsyncSession,
) -> None:
    """incomplete count は open/running のみ・表示窓に依存しない現在値。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add_all(
        [
            # 窓よりはるか昔でも現在値として数える。
            _incomplete(
                source,
                status="open",
                url="https://src.example.com/a",
                created_at=obs - timedelta(days=30),
                ready_at=obs,
            ),
            _incomplete(
                source,
                status="running",
                url="https://src.example.com/b",
                created_at=obs - timedelta(hours=1),
                ready_at=obs,
                leased_until=obs + timedelta(minutes=10),
            ),
            # closed は数えない。
            _incomplete(
                source,
                status="closed",
                url="https://src.example.com/c",
                created_at=obs - timedelta(hours=1),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    # open 1 + running 1、closed 除外。
    assert item.incomplete_count == 2


async def test_last_succeeded_at_max_across_stages_window_independent(
    db_session: AsyncSession,
) -> None:
    """last succeeded at は両 stage の succeeded 最大時刻・窓非依存・failed 無視。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    older = obs - timedelta(days=10)  # 窓外
    newer = obs - timedelta(days=3)  # 窓外だが older より新しい
    db_session.add_all(
        [
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=older,
            ),
            _ev(
                source.id,
                stage=Stage.COMPLETION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_COMPLETED,
                occurred_at=newer,
            ),
            # failed は last_succeeded に影響しない。
            _ev(
                source.id,
                stage=Stage.COMPLETION,
                event_type=EventType.FAILED,
                outcome_code="fetch_timeout",
                occurred_at=obs - timedelta(minutes=1),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    # 窓外でも観測でき、両 stage の最大時刻 = newer。
    assert item.last_succeeded_at == newer


async def test_last_succeeded_at_excludes_non_analyzable_success(
    db_session: AsyncSession,
) -> None:
    """incomplete_article_created (非 analyzable 成功) は last_succeeded に含めない。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    analyzable_at = obs - timedelta(days=3)
    db_session.add_all(
        [
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=analyzable_at,
            ),
            # より新しいが分析可能記事ではない成功 → last_succeeded を進めない。
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_INCOMPLETE_ARTICLE_CREATED,
                occurred_at=obs - timedelta(minutes=1),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    # analyzable 成功の時刻のみ。incomplete の新しい時刻は無視。
    assert item.last_succeeded_at == analyzable_at


async def test_last_succeeded_at_null_when_only_incomplete_success(
    db_session: AsyncSession,
) -> None:
    """analyzable 成功が皆無 (incomplete のみ) の source は last_succeeded が None。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add(
        _ev(
            source.id,
            stage=Stage.ACQUISITION,
            event_type=EventType.SUCCEEDED,
            outcome_code=_INCOMPLETE_ARTICLE_CREATED,
            occurred_at=obs - timedelta(minutes=1),
        )
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    assert item.last_succeeded_at is None


async def test_rate_null_when_no_processed_but_failures_present(
    db_session: AsyncSession,
) -> None:
    """processed=0 では rate は None。failure reasons は別途出る。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add(
        _ev(
            source.id,
            stage=Stage.COMPLETION,
            event_type=EventType.FAILED,
            outcome_code="fetch_timeout",
            occurred_at=obs - timedelta(hours=1),
        )
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    assert item.processed_article_count == 0
    assert item.analyzable_rate is None
    assert [(f.outcome_code, f.count) for f in item.failure_reasons] == [
        ("fetch_timeout", 1)
    ]


async def test_all_sources_listed_including_inactive_and_eventless(
    db_session: AsyncSession,
) -> None:
    """active / inactive / event 無し source がすべて name 昇順で出る。"""
    obs = _OBSERVED_AT
    active = await _make_source(db_session, "Bravo", slug="bravo", is_active=True)
    inactive = await _make_source(db_session, "Alpha", slug="alpha", is_active=False)
    eventless = await _make_source(db_session, "Charlie", slug="charlie")
    db_session.add_all(
        [
            _ev(
                active.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            _ev(
                inactive.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
        ]
    )
    await db_session.flush()

    result = await _health(db_session, observed_at=obs)

    # name 昇順 (Alpha, Bravo, Charlie)。
    assert [i.source_name for i in result.items] == ["Alpha", "Bravo", "Charlie"]
    # inactive も health を持つ。
    assert _item(result, inactive.id).is_active is False
    assert _item(result, inactive.id).analyzable_count == 1
    # event 無し source は 0 / None / [] / None。
    blank = _item(result, eventless.id)
    assert blank.analyzable_count == 0
    assert blank.processed_article_count == 0
    assert blank.analyzable_rate is None
    assert blank.failure_reasons == []
    assert blank.last_succeeded_at is None
    assert blank.incomplete_count == 0


async def test_null_source_id_event_excluded(db_session: AsyncSession) -> None:
    """source_id NULL の event はどの source の集計にも乗らない。"""
    obs = _OBSERVED_AT
    source = await _make_source(db_session, "Src", slug="src")
    db_session.add_all(
        [
            _ev(
                source.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            # source 削除済み (source_id NULL) の event。
            _ev(
                None,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
        ]
    )
    await db_session.flush()

    item = _item(await _health(db_session, observed_at=obs), source.id)

    # NULL source_id の 1 件は混入しない。
    assert item.analyzable_count == 1


async def test_metrics_isolated_per_source(db_session: AsyncSession) -> None:
    """別 source の event は混ざらない。"""
    obs = _OBSERVED_AT
    src1 = await _make_source(db_session, "One", slug="one")
    src2 = await _make_source(db_session, "Two", slug="two")
    db_session.add_all(
        [
            _ev(
                src1.id,
                stage=Stage.ACQUISITION,
                event_type=EventType.SUCCEEDED,
                outcome_code=_ARTICLE_CREATED,
                occurred_at=obs - timedelta(hours=1),
            ),
            _ev(
                src2.id,
                stage=Stage.COMPLETION,
                event_type=EventType.FAILED,
                outcome_code="fetch_timeout",
                occurred_at=obs - timedelta(hours=1),
            ),
        ]
    )
    await db_session.flush()

    result = await _health(db_session, observed_at=obs)

    assert _item(result, src1.id).analyzable_count == 1
    assert _item(result, src1.id).failure_reasons == []
    assert _item(result, src2.id).analyzable_count == 0
    assert _item(result, src2.id).failure_reasons[0].outcome_code == "fetch_timeout"
