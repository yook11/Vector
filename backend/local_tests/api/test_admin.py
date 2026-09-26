"""管理のAPIが、vector_apiの権限でソースの登録・有効化・無効化・削除を行う。"""

from datetime import UTC, datetime

import pytest

from local_tests.api.support import (
    news_source_record,
    news_source_updated_at,
    news_sources_by_name,
    seed_news_source,
)

pytestmark = pytest.mark.asyncio


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
