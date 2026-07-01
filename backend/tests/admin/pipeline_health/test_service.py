"""PipelineHealthService の集計ロジック (integration)。

観測時刻 ``observed_at`` を固定注入して 24h event 窓 / backfill 窓 / queue 集計の
不変条件を確認する。期待値は seed 入力から導出し、除外されるべきデータを必ず
混ぜて非空虚にする。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.pipeline_health.repository import PipelineHealthRepository
from app.admin.pipeline_health.schemas import (
    PipelineHealthResponse,
    PipelineStageHealth,
)
from app.admin.pipeline_health.service import PipelineHealthService
from app.audit.domain.event import EventType, Stage
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.incomplete_article import IncompleteArticle
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent

# 観測基準時刻 (固定注入で窓計算を決定的にする)。
_OBSERVED_AT = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)


async def _health(
    session: AsyncSession, observed_at: datetime
) -> PipelineHealthResponse:
    service = PipelineHealthService(PipelineHealthRepository(session))
    return await service.get_health(observed_at=observed_at)


def _stage(result: PipelineHealthResponse, stage: Stage) -> PipelineStageHealth:
    return next(s for s in result.stages if s.stage == stage)


def _event(stage: Stage, event_type: EventType, occurred_at: datetime) -> PipelineEvent:
    return PipelineEvent(
        stage=stage,
        event_type=event_type,
        outcome_code="ok",
        occurred_at=occurred_at,
    )


def _article(
    source: NewsSource, *, created_at: datetime, url: str
) -> AnalyzableArticleRecord:
    return AnalyzableArticleRecord(
        source_id=source.id,
        source_url=url,
        original_title="t",
        original_content="x" * 60,
        created_at=created_at,
        published_at=created_at,
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


async def test_event_counts_only_succeeded_failed_within_24h(
    db_session: AsyncSession,
) -> None:
    """24h 窓内の succeeded/failed のみ計上 (窓外・skipped は除外)。"""
    obs = _OBSERVED_AT
    db_session.add_all(
        [
            _event(Stage.COMPLETION, EventType.SUCCEEDED, obs - timedelta(hours=1)),
            _event(Stage.COMPLETION, EventType.SUCCEEDED, obs - timedelta(hours=23)),
            _event(Stage.COMPLETION, EventType.FAILED, obs - timedelta(hours=2)),
            # 窓外 (>24h) と skipped は除外される。
            _event(Stage.COMPLETION, EventType.SUCCEEDED, obs - timedelta(hours=25)),
            _event(Stage.COMPLETION, EventType.SKIPPED, obs - timedelta(hours=1)),
        ]
    )
    await db_session.flush()

    completion = _stage(await _health(db_session, obs), Stage.COMPLETION)

    # 窓内 succeeded は 2 件 (窓外 1 / skipped 1 は数えない)。
    assert completion.succeeded_event_count_24h == 2
    assert completion.failed_event_count_24h == 1


async def test_event_counts_grouped_by_stage(db_session: AsyncSession) -> None:
    """イベントが stage ごとに分離集計される。"""
    obs = _OBSERVED_AT
    db_session.add_all(
        [
            _event(Stage.ACQUISITION, EventType.SUCCEEDED, obs - timedelta(hours=1)),
            _event(Stage.ACQUISITION, EventType.SUCCEEDED, obs - timedelta(hours=1)),
            _event(Stage.CURATION, EventType.FAILED, obs - timedelta(hours=1)),
        ]
    )
    await db_session.flush()
    result = await _health(db_session, obs)

    assert _stage(result, Stage.ACQUISITION).succeeded_event_count_24h == 2
    assert _stage(result, Stage.CURATION).failed_event_count_24h == 1
    assert _stage(result, Stage.ACQUISITION).failed_event_count_24h == 0


async def test_all_audit_stages_are_included_and_counted(
    db_session: AsyncSession,
) -> None:
    """全 audit stage が応答に現れ、event summary にも混入する。"""
    obs = _OBSERVED_AT
    db_session.add_all(
        [
            _event(Stage.DISPATCH, EventType.SUCCEEDED, obs - timedelta(hours=1)),
            _event(Stage.BACKFILL_CURATE, EventType.FAILED, obs - timedelta(hours=1)),
            _event(Stage.BRIEFING, EventType.FAILED, obs - timedelta(hours=1)),
        ]
    )
    await db_session.flush()
    result = await _health(db_session, obs)

    assert [s.stage for s in result.stages] == list(Stage)
    assert _stage(result, Stage.DISPATCH).succeeded_event_count_24h == 1
    assert _stage(result, Stage.BACKFILL_CURATE).failed_event_count_24h == 1
    assert _stage(result, Stage.BRIEFING).failed_event_count_24h == 1
    # 全 audit stage projection のため、旧対象外 stage の failed も合計に入る。
    assert result.summary.failed_event_count_24h == 2


async def test_last_succeeded_at_is_latest_succeeded(
    db_session: AsyncSession,
) -> None:
    """lastSucceededAt は最新の succeeded occurred_at (時間下限なし)。"""
    obs = _OBSERVED_AT
    older = obs - timedelta(hours=10)
    newer = obs - timedelta(hours=1)
    db_session.add_all(
        [
            _event(Stage.COMPLETION, EventType.SUCCEEDED, older),
            _event(Stage.COMPLETION, EventType.SUCCEEDED, newer),
            # failed は last_succeeded に影響しない。
            _event(Stage.COMPLETION, EventType.FAILED, obs - timedelta(minutes=1)),
        ]
    )
    await db_session.flush()
    result = await _health(db_session, obs)

    assert _stage(result, Stage.COMPLETION).last_succeeded_at == newer
    assert _stage(result, Stage.CURATION).last_succeeded_at is None


async def test_completion_queue_counts_open_and_running_only(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """completion queue は open/running のみ計上し最古 created_at で age を出す。"""
    obs = _OBSERVED_AT
    db_session.add_all(
        [
            _incomplete(
                sample_source,
                status="open",
                created_at=obs - timedelta(hours=2),
                url="https://example.com/q1",
                ready_at=obs,
            ),
            _incomplete(
                sample_source,
                status="open",
                created_at=obs - timedelta(hours=5),
                url="https://example.com/q2",
                ready_at=obs,
            ),
            _incomplete(
                sample_source,
                status="running",
                created_at=obs - timedelta(hours=1),
                url="https://example.com/q3",
                ready_at=obs,
                leased_until=obs + timedelta(minutes=10),
            ),
            # closed は queue から除外 (最古だが拾わない)。
            _incomplete(
                sample_source,
                status="closed",
                created_at=obs - timedelta(hours=9),
                url="https://example.com/q4",
            ),
        ]
    )
    await db_session.flush()
    result = await _health(db_session, obs)
    completion = _stage(result, Stage.COMPLETION)

    # open 2 + running 1、closed 除外。
    assert completion.queue_count == 3
    # 最古 open/running は obs-5h (closed の 9h ではない)。
    assert completion.oldest_queue_age_seconds == int(
        timedelta(hours=5).total_seconds()
    )
    # queue 軸は COMPLETION 専属: queue が非空でも他 stage は 0/None を返す
    # (completion_count を全 stage へ配る変異をここで非空虚に捕捉する)。
    curation = _stage(result, Stage.CURATION)
    assert curation.queue_count == 0
    assert curation.oldest_queue_age_seconds is None


async def test_backfill_targets_use_backfill_window(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """curation backfill は BackfillWindow (7day/30min) 内のみを対象に数える。"""
    obs = _OBSERVED_AT
    db_session.add_all(
        [
            # 窓内 (curation 子なし AnalyzableArticleRecord)。
            _article(
                sample_source,
                created_at=obs - timedelta(days=1),
                url="https://example.com/bf-in1",
            ),
            _article(
                sample_source,
                created_at=obs - timedelta(days=3),
                url="https://example.com/bf-in2",
            ),
            # grace 未満 (新しすぎ) は対象外。
            _article(
                sample_source,
                created_at=obs - timedelta(minutes=10),
                url="https://example.com/bf-new",
            ),
            # freshness 超過 (古すぎ) は対象外。
            _article(
                sample_source,
                created_at=obs - timedelta(days=8),
                url="https://example.com/bf-old",
            ),
        ]
    )
    await db_session.flush()
    result = await _health(db_session, obs)
    curation = _stage(result, Stage.CURATION)

    # 窓内 2 件のみ (tooNew / tooOld を除外)。
    assert curation.backfill_target_count == 2
    # 最古対象は obs-3d (窓外の 8d は拾わない)。
    assert curation.oldest_backfill_target_age_seconds == int(
        timedelta(days=3).total_seconds()
    )
    # audit の backfill stage 行は event projection であり、target 補助軸は持たない。
    assert _stage(result, Stage.BACKFILL_CURATE).backfill_target_count == 0


async def test_empty_db_returns_zeros_and_nulls(db_session: AsyncSession) -> None:
    """空 DB では全 stage が 0 件・null age、window は注入時刻から導出。"""
    result = await _health(db_session, _OBSERVED_AT)

    for s in result.stages:
        assert s.succeeded_event_count_24h == 0
        assert s.failed_event_count_24h == 0
        assert s.queue_count == 0
        assert s.oldest_queue_age_seconds is None
        assert s.backfill_target_count == 0
        assert s.oldest_backfill_target_age_seconds is None
        assert s.last_succeeded_at is None

    summary = result.summary
    assert summary.failed_event_count_24h == 0
    assert summary.backfill_target_total == 0
    assert summary.oldest_backfill_target_age_seconds is None
    assert summary.completion_queue_count == 0
    assert summary.oldest_completion_queue_age_seconds is None
    assert summary.observed_at == _OBSERVED_AT
    assert summary.event_window_start == _OBSERVED_AT - timedelta(hours=24)


async def test_summary_aggregates_stage_rows(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """summary は stage 行から合算 (failed 合計 / backfill 合計 / queue ミラー)。"""
    obs = _OBSERVED_AT
    db_session.add_all(
        [
            _event(Stage.ACQUISITION, EventType.FAILED, obs - timedelta(hours=1)),
            _event(Stage.COMPLETION, EventType.FAILED, obs - timedelta(hours=1)),
            _event(Stage.CURATION, EventType.FAILED, obs - timedelta(hours=1)),
            _article(
                sample_source,
                created_at=obs - timedelta(days=2),
                url="https://example.com/sum-bf",
            ),
            _incomplete(
                sample_source,
                status="open",
                created_at=obs - timedelta(hours=1),
                url="https://example.com/sum-q",
                ready_at=obs,
            ),
        ]
    )
    await db_session.flush()
    summary = (await _health(db_session, obs)).summary

    # 3 stage に 1 件ずつ failed → 合計 3。
    assert summary.failed_event_count_24h == 3
    # curation backfill 1 (assessment/embedding は 0)。
    assert summary.backfill_target_total == 1
    # completion queue をミラー。
    assert summary.completion_queue_count == 1


async def test_summary_oldest_backfill_is_oldest_across_stages(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """summary の最古 backfill age は複数 stage 横断の最古 (=最大 age)。

    異なる age の backfill 対象を 2 stage に仕込み、max-reduction が最古を選ぶこと
    を確認する (min に取り違えると新しい側を返し落ちる = 非空虚)。
    """
    obs = _OBSERVED_AT
    # curation backfill 対象 (子 curation なし) @ obs-5d。
    curation_target = _article(
        sample_source,
        created_at=obs - timedelta(days=5),
        url="https://example.com/bf-cur-old",
    )
    # assessment backfill 対象になる article (後で curation を付与) @ obs-1d。
    assessment_article = _article(
        sample_source,
        created_at=obs - timedelta(days=1),
        url="https://example.com/bf-assess-new",
    )
    db_session.add_all([curation_target, assessment_article])
    await db_session.flush()
    # curation を付けると curation 対象から外れ、assessment backfill 対象になる。
    db_session.add(
        ArticleCuration(
            analyzable_article_id=assessment_article.id,
            translated_title="tt",
            summary="ss",
        )
    )
    await db_session.flush()

    summary = (await _health(db_session, obs)).summary

    # curation(5d) + assessment(1d) の 2 対象 (両方が在ることで非空虚性を担保)。
    assert summary.backfill_target_total == 2
    # 最古 = 全 stage 横断の最大 age = curation の 5d (min なら 1d で落ちる)。
    assert summary.oldest_backfill_target_age_seconds == int(
        timedelta(days=5).total_seconds()
    )
