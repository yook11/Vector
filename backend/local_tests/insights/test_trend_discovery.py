"""トレンド生成がvector_insightsで分析結果を集計し、snapshotと監査を保存する。"""

from datetime import date, datetime

import pytest

from local_tests.insights.support import (
    JST,
    embedding,
    insights_events,
    seed_analyzed_article,
    seed_trends_snapshot,
    seeded_categories,
    trends_snapshots,
)

pytestmark = pytest.mark.asyncio


async def test_trend_discovery_saves_snapshot_of_mentions_in_last_seven_days(
    system_database, trend_worker, notifier
):
    """直近7日に5記事で挙がった語を集計し、snapshotと成功の監査を保存して通知する。"""
    from app.insights.trend_discovery.taskiq_job import run_trend_discovery

    categories = await seeded_categories(system_database)
    category = categories[0]
    key_point_contents = []
    for number in range(5):
        # 5件すべてにNVIDIA、うち2件にはTSMCも一緒に載せる。
        mentions = [{"surface": "NVIDIA", "type": "company"}]
        if number >= 3:
            mentions.append({"surface": "TSMC", "type": "company"})
        content = f"NVIDIAの要点{number}"
        key_point_contents.append(content)
        await seed_analyzed_article(
            system_database,
            category_id=category.id,
            title=f"trend-{number}",
            published_at=datetime(2026, 9, 24, 10 + number, tzinfo=JST),
            analyzed_at=datetime(2026, 9, 28, 14 - number, tzinfo=JST),
            key_points=[{"content": content, "mentions": mentions}],
            embedding_text=embedding(number / 5),
        )

    await run_trend_discovery(trend_worker)

    snapshots = await trends_snapshots(system_database)
    # 生成時刻はアプリが実行時に決めるため比較から外す。
    assert [
        {
            "window_end": snapshot["window_end"],
            "source_analysis_count": snapshot["source_analysis_count"],
        }
        for snapshot in snapshots
    ] == [{"window_end": date(2026, 9, 27), "source_analysis_count": 5}]
    category_trends = next(
        trends
        for trends in snapshots[0]["bundle"]["categoryTrends"]
        if trends["categoryId"] == category.id
    )
    assert [
        {
            "name": mention["name"],
            "appearanceCount": mention["appearanceCount"],
            "relatedMentions": mention["relatedMentions"],
        }
        for mention in category_trends["mostMentioned"]
    ] == [
        {
            "name": "NVIDIA",
            "appearanceCount": 5,
            "relatedMentions": [
                {"name": "TSMC", "type": "company", "sharedArticleCount": 2}
            ],
        }
    ]
    key_points = category_trends["mostMentioned"][0]["keyPoints"]
    assert key_points == list(reversed(key_point_contents[-3:]))
    assert await insights_events(system_database) == [
        {
            "stage": "trend_discovery",
            "event_type": "succeeded",
            "outcome_code": "trend_discovery_run_completed",
            "retryability": None,
            "error_class": None,
            "payload": {
                "kind": "trend_discovery",
                "window_start": "2026-09-20",
                "window_end": "2026-09-27",
                "source_analysis_count": 5,
                "completed_category_count": len(categories),
            },
        }
    ]
    assert notifier.tags == [("trends",)]


async def test_trend_discovery_keeps_snapshot_already_saved_for_the_window(
    system_database, trend_worker, notifier
):
    """同じ期間のsnapshotが保存済みなら、そのsnapshotを残して監査も通知もしない。"""
    from app.insights.trend_discovery.taskiq_job import run_trend_discovery

    category = (await seeded_categories(system_database))[0]
    await seed_analyzed_article(
        system_database,
        category_id=category.id,
        title="trend-after-snapshot",
        analyzed_at=datetime(2026, 9, 26, 9, tzinfo=JST),
    )
    await seed_trends_snapshot(
        system_database,
        window_end=date(2026, 9, 27),
        bundle={"marker": "先に保存したトレンド"},
        source_analysis_count=3,
        generated_at=datetime(2026, 9, 27, 0, 5, tzinfo=JST),
    )

    await run_trend_discovery(trend_worker)

    assert await trends_snapshots(system_database) == [
        {
            "window_end": date(2026, 9, 27),
            "bundle": {"marker": "先に保存したトレンド"},
            "source_analysis_count": 3,
            "generated_at": datetime(2026, 9, 27, 0, 5, tzinfo=JST),
        }
    ]
    assert await insights_events(system_database) == []
    assert notifier.tags == []


async def test_trend_discovery_leaves_snapshot_saved_by_another_worker(
    system_database, trend_worker
):
    """生成済みかの確認の後に別workerが保存していたら、競合として既存行を残す。"""
    from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery
    from app.insights.trend_discovery.service import (
        TrendDiscoveryConflict,
        TrendDiscoveryService,
    )

    categories = await seeded_categories(system_database)
    await seed_analyzed_article(
        system_database,
        category_id=categories[0].id,
        title="trend-race",
        analyzed_at=datetime(2026, 9, 26, 9, tzinfo=JST),
    )
    await seed_trends_snapshot(
        system_database,
        window_end=date(2026, 9, 27),
        bundle={"marker": "別workerが保存したトレンド"},
        source_analysis_count=1,
        generated_at=datetime(2026, 9, 27, 0, 5, tzinfo=JST),
    )
    service = TrendDiscoveryService(trend_worker.state.session_factory)

    outcome = await service.execute(
        ReadyForTrendDiscovery(window_end=date(2026, 9, 27))
    )

    assert outcome == TrendDiscoveryConflict(
        window_end=date(2026, 9, 27),
        source_analysis_count=1,
        completed_category_count=len(categories),
    )
    assert await trends_snapshots(system_database) == [
        {
            "window_end": date(2026, 9, 27),
            "bundle": {"marker": "別workerが保存したトレンド"},
            "source_analysis_count": 1,
            "generated_at": datetime(2026, 9, 27, 0, 5, tzinfo=JST),
        }
    ]
