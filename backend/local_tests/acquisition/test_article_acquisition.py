"""取得記事の充足状態に応じた保存先と後続イベントを確認する。"""

import pytest

from local_tests.acquisition.support import (
    load_completed_articles,
    load_incomplete_articles,
    load_stored_events,
    rss_article_response,
)

pytestmark = pytest.mark.asyncio


async def test_complete_article_is_saved_with_created_event(
    system_database, invoke_acquisition, source_id, rss_response
):
    """本文が揃った記事を完成記事として保存し、対応する完成イベントを記録する。"""
    title = "Acquired article"
    url = "https://venturebeat.com/acquisition-complete"
    body = ("Acquired content. " * 20).strip()
    rss_response.return_value = rss_article_response(title=title, url=url, body=body)

    response = await invoke_acquisition(source_id)

    articles = await load_completed_articles(system_database)
    assert len(articles) == 1
    article = articles[0]
    assert response == {"batchItemFailures": []}
    assert article["source_id"] == source_id
    assert article["source_url"] == url
    assert article["original_title"] == title
    assert article["original_content"] == body
    assert await load_incomplete_articles(system_database) == []

    events = await load_stored_events(system_database)
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "article.analyzable_created"
    assert event["schema_version"] == 1
    assert event["payload"] == {"analyzable_article_id": article["id"]}


async def test_incomplete_article_is_saved_with_completion_event(
    system_database, invoke_acquisition, source_id, rss_response
):
    """本文不足の記事を未完成記事として保存し、対応する補完依頼イベントを記録する。"""
    title = "Incomplete article"
    url = "https://venturebeat.com/acquisition-incomplete"
    rss_response.return_value = rss_article_response(title=title, url=url, body=None)

    response = await invoke_acquisition(source_id)

    assert response == {"batchItemFailures": []}
    assert await load_completed_articles(system_database) == []
    articles = await load_incomplete_articles(system_database)
    assert len(articles) == 1
    article = articles[0]
    assert article["source_id"] == source_id
    assert article["url"] == url
    assert article["observed_article"]["title"]["value"] == title
    assert article["observed_article"]["body"] is None

    events = await load_stored_events(system_database)
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "article.incomplete_recorded"
    assert event["schema_version"] == 1
    assert event["payload"] == {
        "incomplete_article_id": article["id"],
        "source_id": source_id,
    }


async def test_redelivery_after_committed_article_does_not_repeat_event(
    system_database, invoke_acquisition, source_id, rss_response
):
    """commit済みの応答を失って再配送されても記事とイベントは一つだけ残る。"""
    rss_response.return_value = rss_article_response(
        title="Repeated article",
        url="https://venturebeat.com/repeated",
        body="Article content. " * 40,
    )
    await invoke_acquisition(source_id)
    response = await invoke_acquisition(source_id)
    assert response == {"batchItemFailures": []}
    assert rss_response.await_count == 2
    assert len(await load_completed_articles(system_database)) == 1
    assert len(await load_stored_events(system_database)) == 1


async def test_redelivery_of_incomplete_article_does_not_repeat_event(
    system_database, invoke_acquisition, source_id, rss_response
):
    """補完待ち記事の再取得でも補完依頼イベントを増やさない。"""
    rss_response.return_value = rss_article_response(
        title="Incomplete repeated",
        url="https://venturebeat.com/incomplete-repeated",
        body=None,
    )
    await invoke_acquisition(source_id)
    assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    assert len(await load_incomplete_articles(system_database)) == 1
    assert len(await load_stored_events(system_database)) == 1


async def test_inactive_source_is_acknowledged_without_fetch(
    system_database, invoke_acquisition, source_id, rss_response
):
    """開始前に無効化されたソースは再配信せず取得を終える。"""
    async with system_database.connect("vector") as db:
        await db.execute(
            "UPDATE news_sources SET is_active=false WHERE id=$1", source_id
        )
    assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    rss_response.assert_not_awaited()


async def test_deleted_source_is_acknowledged_without_fetch(
    system_database, invoke_acquisition, source_id, rss_response
):
    """開始前に削除されたソースは再配信せず取得を終える。"""
    async with system_database.connect("vector") as db:
        await db.execute("DELETE FROM news_sources WHERE id=$1", source_id)
    assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    rss_response.assert_not_awaited()


@pytest.mark.parametrize("failure", ["timeout", "not_found"])
async def test_fetch_failure_is_redelivered_and_can_recover(
    system_database, invoke_acquisition, source_id, rss_response, failure
):
    """取得の失敗はSQSへ返し、次の配送で回復できる。"""
    import httpx

    rss_response.side_effect = (
        httpx.ReadTimeout("private-fetch-detail")
        if failure == "timeout"
        else httpx.HTTPStatusError(
            "private-fetch-detail",
            request=httpx.Request("GET", "https://venturebeat.com/feed"),
            response=httpx.Response(404),
        )
    )
    assert await invoke_acquisition(source_id) == {
        "batchItemFailures": [{"itemIdentifier": "acquisition-message"}]
    }
    assert await load_stored_events(system_database) == []
    rss_response.side_effect = None
    rss_response.return_value = rss_article_response(
        title="Recovered article",
        url="https://venturebeat.com/recovered",
        body="Recovered body. " * 40,
    )
    assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    assert len(await load_stored_events(system_database)) == 1


async def test_unregistered_source_is_failed_until_definition_is_repaired(
    system_database, invoke_acquisition, source_id, rss_response
):
    """登録に解決できない有効ソースは成功扱いにせず、修復後の再配送で取得する。"""
    async with system_database.connect("vector") as db:
        await db.execute(
            "UPDATE news_sources SET name='Missing acquisition definition' WHERE id=$1",
            source_id,
        )
    assert await invoke_acquisition(source_id) == {
        "batchItemFailures": [{"itemIdentifier": "acquisition-message"}]
    }
    rss_response.assert_not_awaited()
    async with system_database.connect("vector") as db:
        await db.execute(
            "UPDATE news_sources SET name='VentureBeat' WHERE id=$1", source_id
        )
    rss_response.return_value = rss_article_response(
        title="Repaired source",
        url="https://venturebeat.com/repaired",
        body="Article body. " * 40,
    )
    assert await invoke_acquisition(source_id) == {"batchItemFailures": []}


async def test_database_read_failure_is_redelivered_after_permissions_recover(
    system_database, invoke_acquisition, source_id, rss_response
):
    """DBのソース確認障害を空の正常結果へ変換しない。"""
    async with system_database.connect("vector") as db:
        await db.execute("REVOKE SELECT ON news_sources FROM vector_collect")
    try:
        assert await invoke_acquisition(source_id) == {
            "batchItemFailures": [{"itemIdentifier": "acquisition-message"}]
        }
        rss_response.assert_not_awaited()
    finally:
        async with system_database.connect("vector") as db:
            await db.execute("GRANT SELECT ON news_sources TO vector_collect")
    rss_response.return_value = rss_article_response(
        title="DB recovered",
        url="https://venturebeat.com/db-recovered",
        body="Article body. " * 40,
    )
    assert await invoke_acquisition(source_id) == {"batchItemFailures": []}


async def test_concurrent_requests_persist_one_article_and_event(
    system_database, invoke_acquisition, source_id, rss_response
):
    """同じ取得依頼が同時に実行されても記事と後続イベントは一度だけ保存する。"""
    import asyncio

    rss_response.return_value = rss_article_response(
        title="Concurrent article",
        url="https://venturebeat.com/concurrent",
        body="Concurrent article body. " * 40,
    )
    responses = await asyncio.gather(
        invoke_acquisition(source_id), invoke_acquisition(source_id)
    )
    assert responses == [{"batchItemFailures": []}, {"batchItemFailures": []}]
    assert rss_response.await_count == 2
    assert len(await load_completed_articles(system_database)) == 1
    assert len(await load_stored_events(system_database)) == 1
