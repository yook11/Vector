"""ウォッチリストのAPIが、vector_apiの権限で利用者のウォッチを追加・参照・解除する。"""

from datetime import UTC, datetime

import pytest

from local_tests.api.support import (
    seed_article,
    seeded_categories,
    seeded_source,
    watch,
    watched_article_ids,
)

pytestmark = pytest.mark.asyncio


async def _seed_article(database, name):
    [category, *_] = await seeded_categories(database)
    return await seed_article(
        database,
        source=await seeded_source(database),
        category=category,
        source_url=f"https://example.com/{name}",
        title=name,
    )


async def test_watching_article_saves_entry_for_user(
    api_client, user_headers, user_id, system_database
):
    """記事をウォッチすると、その利用者のウォッチとして保存される。"""
    article_id = await _seed_article(system_database, "watched")

    response = await api_client.post(
        "/api/v1/me/watchlist", json={"articleId": article_id}, headers=user_headers
    )

    assert response.status_code == 201
    assert await watched_article_ids(system_database, user_id) == [article_id]


async def test_watchlist_ids_are_newest_first(
    api_client, user_headers, user_id, system_database
):
    """ID一覧は、ウォッチ中の記事だけを新しくウォッチした順に返す。"""
    older = await _seed_article(system_database, "older")
    newer = await _seed_article(system_database, "newer")
    await _seed_article(system_database, "unwatched")
    await watch(
        system_database,
        user_id=user_id,
        article_id=older,
        watched_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    await watch(
        system_database,
        user_id=user_id,
        article_id=newer,
        watched_at=datetime(2026, 9, 21, tzinfo=UTC),
    )

    response = await api_client.get("/api/v1/me/watchlist/ids", headers=user_headers)

    assert response.status_code == 200
    assert response.json() == {"ids": [newer, older]}


async def test_watchlist_returns_watched_articles_newest_first(
    api_client, user_headers, user_id, system_database
):
    """一覧は、ウォッチ中の記事だけを新しくウォッチした順に返す。"""
    older = await _seed_article(system_database, "older")
    newer = await _seed_article(system_database, "newer")
    await _seed_article(system_database, "unwatched")
    await watch(
        system_database,
        user_id=user_id,
        article_id=older,
        watched_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    await watch(
        system_database,
        user_id=user_id,
        article_id=newer,
        watched_at=datetime(2026, 9, 21, tzinfo=UTC),
    )

    response = await api_client.get("/api/v1/me/watchlist", headers=user_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [newer, older]


async def test_unwatching_article_removes_entry(
    api_client, user_headers, user_id, system_database
):
    """ウォッチを解除すると、その利用者のウォッチから消える。"""
    article_id = await _seed_article(system_database, "watched")
    await watch(
        system_database,
        user_id=user_id,
        article_id=article_id,
        watched_at=datetime(2026, 9, 20, tzinfo=UTC),
    )

    response = await api_client.delete(
        f"/api/v1/me/watchlist/{article_id}", headers=user_headers
    )

    assert response.status_code == 204
    assert await watched_article_ids(system_database, user_id) == []
