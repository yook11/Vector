"""パイプラインの健全性が、工程ごとの直近の成否・補完待ち・救済対象を示す。

画面はAPIを通して確かめ、集計の規則は観測時刻を固定してserviceを直接呼ぶ。
どちらもvector_apiの権限で読む。
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.admin.pipeline_health.repository import PipelineHealthRepository
from app.admin.pipeline_health.schemas import (
    PipelineHealthResponse,
    PipelineHealthSummary,
    PipelineStageHealth,
)
from app.admin.pipeline_health.service import PipelineHealthService
from app.audit.domain.event import Stage
from local_tests.api.support import (
    SeededCategory,
    SeededSource,
    api_time,
    embedding,
    seed_analyzable_article,
    seed_article,
    seed_assessment_exclusion,
    seed_curation,
    seed_curation_noise,
    seed_embedding_exclusion,
    seed_incomplete_article,
    seed_out_of_scope,
    seed_pipeline_event,
    seeded_categories,
    seeded_source,
)

pytestmark = pytest.mark.asyncio

_OBSERVED_AT = datetime(2026, 9, 20, 12, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


async def _health(session) -> PipelineHealthResponse:
    service = PipelineHealthService(PipelineHealthRepository(session))
    return await service.get_health(observed_at=_OBSERVED_AT)


async def _awaiting_curation(
    database, source: SeededSource, url: str, created_at: datetime
) -> int:
    return await seed_analyzable_article(
        database,
        source=source,
        source_url=url,
        title="curation待ちの記事",
        created_at=created_at,
    )


async def _awaiting_assessment(
    database, source: SeededSource, url: str, created_at: datetime
) -> int:
    article_id = await _awaiting_curation(database, source, url, created_at)
    return await seed_curation(
        database, analyzable_article_id=article_id, title="判定待ちの記事"
    )


async def _awaiting_embedding(
    database,
    source: SeededSource,
    category: SeededCategory,
    url: str,
    created_at: datetime,
) -> int:
    return await seed_article(
        database,
        source=source,
        category=category,
        source_url=url,
        title="埋め込み待ちの記事",
        created_at=created_at,
    )


async def test_pipeline_health_shows_where_articles_are_waiting(
    api_client, admin_headers, system_database
):
    """パイプラインの健全性は、工程ごとの直近の成否・補完待ち・進んでいない記事を示す。"""
    now = datetime.now(UTC).replace(microsecond=0)
    source = await seeded_source(system_database)
    [category, *_] = await seeded_categories(system_database)
    # 直近24時間の成否と、24時間より前に最後に成功した工程
    for stage, event_type, outcome_code, occurred_at in (
        ("curation", "succeeded", "curated", now - timedelta(hours=1)),
        ("assessment", "failed", "ai_unavailable", now - timedelta(hours=1)),
        ("embedding", "succeeded", "embedded", now - timedelta(days=3)),
    ):
        await seed_pipeline_event(
            system_database,
            stage=stage,
            event_type=event_type,
            outcome_code=outcome_code,
            occurred_at=occurred_at,
        )
    # 補完を待つ未完成記事と、打ち切り済みの未完成記事
    for status in ("open", "closed"):
        await seed_incomplete_article(
            system_database,
            source=source,
            url=f"https://example.com/incomplete-{status}",
            status=status,
            created_at=now - timedelta(hours=2),
        )
    # 30分の猶予を過ぎても次の工程へ進んでいない記事と、進まなくてよい記事
    await seed_analyzable_article(
        system_database,
        source=source,
        source_url="https://example.com/uncurated",
        title="整形待ちの記事",
        created_at=now - timedelta(hours=1),
    )
    await seed_analyzable_article(
        system_database,
        source=source,
        source_url="https://example.com/just-acquired",
        title="取得直後の記事",
        created_at=now - timedelta(minutes=5),
    )
    unassessed = await seed_analyzable_article(
        system_database,
        source=source,
        source_url="https://example.com/unassessed",
        title="判定待ちの記事",
        created_at=now - timedelta(hours=1),
    )
    await seed_curation(
        system_database, analyzable_article_id=unassessed, title="判定待ちの記事"
    )
    judged = await seed_analyzable_article(
        system_database,
        source=source,
        source_url="https://example.com/out-of-scope",
        title="対象外と判定した記事",
        created_at=now - timedelta(hours=1),
    )
    judged_curation = await seed_curation(
        system_database, analyzable_article_id=judged, title="対象外と判定した記事"
    )
    await seed_out_of_scope(system_database, curation_id=judged_curation)
    await seed_article(
        system_database,
        source=source,
        category=category,
        source_url="https://example.com/unembedded",
        title="埋め込み待ちの記事",
        created_at=now - timedelta(hours=1),
    )

    response = await api_client.get(
        "/api/v1/admin/pipeline/health", headers=admin_headers
    )

    assert response.status_code == 200
    body = response.json()
    # 工程ごとに（直近24時間の成功, 失敗, 補完待ち, 進んでいない記事）の件数を比べる。
    counts = {
        stage["stage"]: (
            stage["succeededEventCount24h"],
            stage["failedEventCount24h"],
            stage["queueCount"],
            stage["backfillTargetCount"],
        )
        for stage in body["stages"]
    }
    assert counts == {stage.value: (0, 0, 0, 0) for stage in Stage} | {
        "curation": (1, 0, 0, 1),
        "assessment": (0, 1, 0, 1),
        "embedding": (0, 0, 0, 1),
        "completion": (0, 0, 1, 0),
    }
    last_succeeded = {
        stage["stage"]: stage["lastSucceededAt"]
        for stage in body["stages"]
        if stage["lastSucceededAt"] is not None
    }
    assert last_succeeded == {
        "curation": api_time(now - timedelta(hours=1)),
        "embedding": api_time(now - timedelta(days=3)),
    }
    summary = body["summary"]
    assert summary["failedEventCount24h"] == 1
    assert summary["backfillTargetTotal"] == 3
    assert summary["completionQueueCount"] == 1
    # 経過秒は観測時刻に依存するため、置いた時刻からの経過を幅で確かめる。
    assert 2 * 3600 <= summary["oldestCompletionQueueAgeSeconds"] < 2 * 3600 + 60
    assert 3600 <= summary["oldestBackfillTargetAgeSeconds"] < 3600 + 60


async def test_events_are_counted_per_stage_within_24_hours(
    api_session, system_database
):
    """直近24時間の成功と失敗を工程ごとに数え、窓より前とskippedは数えない。"""
    window_start = _OBSERVED_AT - timedelta(hours=24)
    for stage, event_type, occurred_at in (
        ("completion", "succeeded", window_start),
        ("completion", "succeeded", _OBSERVED_AT - timedelta(hours=1)),
        ("completion", "failed", _OBSERVED_AT - timedelta(hours=2)),
        ("acquisition", "succeeded", _OBSERVED_AT - timedelta(hours=1)),
        ("backfill_curate", "failed", _OBSERVED_AT - timedelta(hours=1)),
        ("briefing", "failed", _OBSERVED_AT - timedelta(hours=1)),
        ("completion", "succeeded", window_start - _MICROSECOND),
        ("completion", "skipped", _OBSERVED_AT - timedelta(hours=1)),
    ):
        await seed_pipeline_event(
            system_database,
            stage=stage,
            event_type=event_type,
            outcome_code="ok",
            occurred_at=occurred_at,
        )

    result = await _health(api_session)

    # 工程ごとに（成功, 失敗）の件数を、全工程の定義順で比べる。
    counted = {
        Stage.COMPLETION: (2, 1),
        Stage.ACQUISITION: (1, 0),
        Stage.BACKFILL_CURATE: (0, 1),
        Stage.BRIEFING: (0, 1),
    }
    assert [
        (stage.stage, stage.succeeded_event_count_24h, stage.failed_event_count_24h)
        for stage in result.stages
    ] == [(stage, *counted.get(stage, (0, 0))) for stage in Stage]


async def test_last_success_is_latest_success_regardless_of_window(
    api_session, system_database
):
    """最後の成功は、24時間の窓に関係なく工程ごとの最新の成功時刻で、失敗は含まない。"""
    for stage, event_type, occurred_at in (
        ("completion", "succeeded", _OBSERVED_AT - timedelta(hours=10)),
        ("completion", "succeeded", _OBSERVED_AT - timedelta(hours=1)),
        ("completion", "failed", _OBSERVED_AT - timedelta(minutes=1)),
        ("curation", "succeeded", _OBSERVED_AT - timedelta(days=3)),
        ("acquisition", "failed", _OBSERVED_AT - timedelta(hours=1)),
    ):
        await seed_pipeline_event(
            system_database,
            stage=stage,
            event_type=event_type,
            outcome_code="ok",
            occurred_at=occurred_at,
        )

    result = await _health(api_session)

    assert {
        stage.stage: stage.last_succeeded_at
        for stage in result.stages
        if stage.last_succeeded_at is not None
    } == {
        Stage.COMPLETION: _OBSERVED_AT - timedelta(hours=1),
        Stage.CURATION: _OBSERVED_AT - timedelta(days=3),
    }


async def test_completion_queue_counts_waiting_and_running_articles(
    api_session, system_database
):
    """補完待ちはopenとrunningの未完成記事を数え、最も古い記事からの経過時間を示す。"""
    source = await seeded_source(system_database)
    for status, hours, leased_until in (
        ("open", 2, None),
        ("open", 5, None),
        ("running", 1, _OBSERVED_AT + timedelta(minutes=10)),
        # 補完を打ち切った記事は、最も古くても数えない。
        ("closed", 9, None),
    ):
        await seed_incomplete_article(
            system_database,
            source=source,
            url=f"https://example.com/incomplete-{status}-{hours}",
            status=status,
            created_at=_OBSERVED_AT - timedelta(hours=hours),
            leased_until=leased_until,
        )

    result = await _health(api_session)

    # 補完待ちは補完の工程だけが持つ。
    queue = {Stage.COMPLETION: (3, int(timedelta(hours=5).total_seconds()))}
    assert [
        (stage.stage, stage.queue_count, stage.oldest_queue_age_seconds)
        for stage in result.stages
    ] == [(stage, *queue.get(stage, (0, None))) for stage in Stage]


async def test_backfill_targets_are_unfinished_articles_within_window(
    api_session, system_database
):
    """救済対象は、取得から30分を過ぎて7日以内の、工程を終えていない記事だけを数える。"""
    source = await seeded_source(system_database)
    [category, *_] = await seeded_categories(system_database)
    oldest = _OBSERVED_AT - timedelta(days=7)
    newest = _OBSERVED_AT - timedelta(minutes=30)
    # 窓の両端の前後に置く。7日前ちょうどは含み、30分前ちょうどは含まない。
    for index, created_at in enumerate(
        (oldest - _MICROSECOND, oldest, newest - _MICROSECOND, newest)
    ):
        await _awaiting_curation(
            system_database, source, f"https://example.com/curation-{index}", created_at
        )
        await _awaiting_assessment(
            system_database,
            source,
            f"https://example.com/assessment-{index}",
            created_at,
        )
        await _awaiting_embedding(
            system_database,
            source,
            category,
            f"https://example.com/embedding-{index}",
            created_at,
        )
    # 全工程を終えた記事と、各工程で救済から外した記事は数えない。
    await seed_article(
        system_database,
        source=source,
        category=category,
        source_url="https://example.com/finished",
        title="全工程を終えた記事",
        embedding_text=embedding(1.0, 0.0),
        created_at=oldest,
    )
    noise = await _awaiting_curation(
        system_database, source, "https://example.com/noise", oldest
    )
    await seed_curation_noise(system_database, analyzable_article_id=noise)
    aged_out_curation = await _awaiting_assessment(
        system_database, source, "https://example.com/assessment-aged-out", oldest
    )
    await seed_assessment_exclusion(system_database, curation_id=aged_out_curation)
    out_of_scope = await _awaiting_assessment(
        system_database, source, "https://example.com/out-of-scope", oldest
    )
    await seed_out_of_scope(system_database, curation_id=out_of_scope)
    aged_out_analysis = await _awaiting_embedding(
        system_database,
        source,
        category,
        "https://example.com/embedding-aged-out",
        oldest,
    )
    await seed_embedding_exclusion(
        system_database, analyzed_article_id=aged_out_analysis
    )

    result = await _health(api_session)

    # 救済対象は整形・判定・埋め込みの工程だけが持つ。
    seven_days = int(timedelta(days=7).total_seconds())
    targets = {
        Stage.CURATION: (2, seven_days),
        Stage.ASSESSMENT: (2, seven_days),
        Stage.EMBEDDING: (2, seven_days),
    }
    assert [
        (
            stage.stage,
            stage.backfill_target_count,
            stage.oldest_backfill_target_age_seconds,
        )
        for stage in result.stages
    ] == [(stage, *targets.get(stage, (0, None))) for stage in Stage]


async def test_summary_totals_stages_and_takes_oldest_backfill_target(
    api_session, system_database
):
    """サマリーは工程の失敗と救済対象を合計し、救済対象の最古は全工程で最も古い記事とする。"""
    source = await seeded_source(system_database)
    for stage in ("acquisition", "completion", "curation"):
        await seed_pipeline_event(
            system_database,
            stage=stage,
            event_type="failed",
            outcome_code="ok",
            occurred_at=_OBSERVED_AT - timedelta(hours=1),
        )
    await _awaiting_curation(
        system_database,
        source,
        "https://example.com/older-target",
        _OBSERVED_AT - timedelta(days=5),
    )
    await _awaiting_assessment(
        system_database,
        source,
        "https://example.com/newer-target",
        _OBSERVED_AT - timedelta(days=1),
    )
    await seed_incomplete_article(
        system_database,
        source=source,
        url="https://example.com/incomplete",
        status="open",
        created_at=_OBSERVED_AT - timedelta(hours=1),
    )

    result = await _health(api_session)

    assert result.summary == PipelineHealthSummary(
        failed_event_count_24h=3,
        backfill_target_total=2,
        oldest_backfill_target_age_seconds=int(timedelta(days=5).total_seconds()),
        completion_queue_count=1,
        oldest_completion_queue_age_seconds=int(timedelta(hours=1).total_seconds()),
        observed_at=_OBSERVED_AT,
        event_window_start=_OBSERVED_AT - timedelta(hours=24),
    )


async def test_no_activity_shows_zero_for_every_stage(api_session):
    """監査・記事・未完成記事が無ければ、全工程が0件で、経過時間と最後の成功は空になる。"""
    result = await _health(api_session)

    assert result == PipelineHealthResponse(
        summary=PipelineHealthSummary(
            failed_event_count_24h=0,
            backfill_target_total=0,
            oldest_backfill_target_age_seconds=None,
            completion_queue_count=0,
            oldest_completion_queue_age_seconds=None,
            observed_at=_OBSERVED_AT,
            event_window_start=_OBSERVED_AT - timedelta(hours=24),
        ),
        stages=[
            PipelineStageHealth(
                stage=stage,
                succeeded_event_count_24h=0,
                failed_event_count_24h=0,
                queue_count=0,
                oldest_queue_age_seconds=None,
                backfill_target_count=0,
                oldest_backfill_target_age_seconds=None,
                last_succeeded_at=None,
            )
            for stage in Stage
        ],
    )
