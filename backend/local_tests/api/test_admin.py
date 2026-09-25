"""管理のAPIが、vector_apiの権限でソースの管理・健全性の表示・手動取得を行う。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.audit.domain.event import Stage
from local_tests.api.support import (
    news_source_record,
    news_source_updated_at,
    news_sources_by_name,
    seed_analyzable_article,
    seed_article,
    seed_curation,
    seed_incomplete_article,
    seed_news_source,
    seed_out_of_scope,
    seed_pipeline_event,
    seeded_categories,
    seeded_source,
)

pytestmark = pytest.mark.asyncio


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def test_source_list_returns_sources_by_name(
    api_client, admin_headers, system_database
):
    """ソースの一覧は、登録済みのソースを名前順に返す。"""
    sources = await news_sources_by_name(system_database)

    response = await api_client.get("/api/v1/admin/sources", headers=admin_headers)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [
        source["id"] for source in sources
    ]


async def test_registering_source_saves_it_as_active(
    api_client, admin_headers, system_database
):
    """ソースを登録すると、送った内容で有効なソースとして保存される。"""
    response = await api_client.post(
        "/api/v1/admin/sources",
        json={
            "name": "Registered Source",
            "sourceType": "rss",
            "siteUrl": "https://registered.example.com/news",
            "endpointUrl": "https://registered.example.com/feed.xml",
        },
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert await news_source_record(system_database, response.json()["id"]) == {
        "name": "Registered Source",
        "source_type": "rss",
        "site_url": "https://registered.example.com/news",
        "endpoint_url": "https://registered.example.com/feed.xml",
        "is_active": True,
    }


@pytest.mark.parametrize(
    ("action", "was_active", "is_active"),
    [("deactivate", True, False), ("activate", False, True)],
)
async def test_toggling_source_switches_active_state(
    api_client, admin_headers, system_database, action, was_active, is_active
):
    """ソースを有効化・無効化すると、有効状態が切り替わり更新時刻が進む。"""
    last_updated_at = datetime(2026, 9, 20, tzinfo=UTC)
    source = await seed_news_source(
        system_database,
        name="Toggled Source",
        endpoint_url="https://toggled.example.com/feed.xml",
        is_active=was_active,
        updated_at=last_updated_at,
    )

    response = await api_client.patch(
        f"/api/v1/admin/sources/{source.id}/{action}", headers=admin_headers
    )

    assert response.status_code == 200
    assert response.json()["isActive"] is is_active
    record = await news_source_record(system_database, source.id)
    assert record["is_active"] is is_active
    assert await news_source_updated_at(system_database, source.id) > last_updated_at


async def test_deleting_source_without_articles_removes_it(
    api_client, admin_headers, system_database
):
    """記事を持たないソースを削除すると、行が消える。"""
    source = await seed_news_source(
        system_database,
        name="Deleted Source",
        endpoint_url="https://deleted.example.com/feed.xml",
    )

    response = await api_client.delete(
        f"/api/v1/admin/sources/{source.id}", headers=admin_headers
    )

    assert response.status_code == 204
    assert await news_source_record(system_database, source.id) is None


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
        "curation": _iso(now - timedelta(hours=1)),
        "embedding": _iso(now - timedelta(days=3)),
    }
    summary = body["summary"]
    assert summary["failedEventCount24h"] == 1
    assert summary["backfillTargetTotal"] == 3
    assert summary["completionQueueCount"] == 1
    # 経過秒は観測時刻に依存するため、置いた時刻からの経過を幅で確かめる。
    assert 2 * 3600 <= summary["oldestCompletionQueueAgeSeconds"] < 2 * 3600 + 60
    assert 3600 <= summary["oldestBackfillTargetAgeSeconds"] < 3600 + 60


async def test_source_health_summarizes_each_source(
    api_client, admin_headers, system_database
):
    """ソースの健全性は、ソースごとに表示窓の取得結果と未完成・最終成功を示す。"""
    now = datetime.now(UTC).replace(microsecond=0)
    source = await seed_news_source(
        system_database,
        name="Health Checked Source",
        endpoint_url="https://health-checked.example.com/feed.xml",
    )
    # 未完成記事を作っただけの成功と24時間より前の成功は、分析可能に数えない。
    for stage, event_type, outcome_code, occurred_at in (
        ("acquisition", "succeeded", "article_created", now - timedelta(hours=2)),
        ("completion", "succeeded", "article_completed", now - timedelta(hours=1)),
        (
            "acquisition",
            "succeeded",
            "incomplete_article_created",
            now - timedelta(hours=1),
        ),
        ("acquisition", "rejected", "not_article", now - timedelta(hours=1)),
        ("acquisition", "failed", "fetch_timeout", now - timedelta(hours=1)),
        ("acquisition", "succeeded", "article_created", now - timedelta(days=3)),
    ):
        await seed_pipeline_event(
            system_database,
            stage=stage,
            event_type=event_type,
            outcome_code=outcome_code,
            occurred_at=occurred_at,
            source_id=source.id,
        )
    for status in ("open", "closed"):
        await seed_incomplete_article(
            system_database,
            source=source,
            url=f"https://health-checked.example.com/incomplete-{status}",
            status=status,
            created_at=now - timedelta(hours=1),
        )

    response = await api_client.get(
        "/api/v1/admin/sources/health", headers=admin_headers
    )

    assert response.status_code == 200
    checked = {
        "analyzableRate": 66.7,
        "analyzableCount": 2,
        "processedArticleCount": 3,
        "incompleteCount": 1,
        "failureReasons": [
            {"outcomeCode": "fetch_timeout", "count": 1},
            {"outcomeCode": "not_article", "count": 1},
        ],
        "lastSucceededAt": _iso(now - timedelta(hours=1)),
    }
    quiet = {
        "analyzableRate": None,
        "analyzableCount": 0,
        "processedArticleCount": 0,
        "incompleteCount": 0,
        "failureReasons": [],
        "lastSucceededAt": None,
    }
    assert response.json()["items"] == [
        {
            "sourceId": row["id"],
            "sourceName": row["name"],
            "sourceType": row["source_type"],
            "isActive": row["is_active"],
            **(checked if row["id"] == source.id else quiet),
        }
        for row in await news_sources_by_name(system_database)
    ]


async def test_fetching_specified_source_enqueues_acquisition(
    api_client, admin_headers, acquisition_requests, system_database
):
    """ソースを指定して取得を依頼すると、そのソースの取得が投入される。"""
    source = await seeded_source(system_database)

    response = await api_client.post(
        "/api/v1/admin/pipeline/fetch",
        json={"sourceIds": [source.id]},
        headers=admin_headers,
    )

    assert response.status_code == 202
    assert response.json() == {
        "message": "Fetch tasks submitted",
        "dispatchedCount": 1,
        "jobId": None,
    }
    assert acquisition_requests == [(source.id, source.name)]
