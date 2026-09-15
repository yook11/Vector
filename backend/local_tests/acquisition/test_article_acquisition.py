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
    system_database, acquisition_service, source_id, rss_response
):
    """本文が揃った記事を完成記事として保存し、対応する完成イベントを記録する。"""
    title = "Acquired article"
    url = "https://venturebeat.com/acquisition-complete"
    body = ("Acquired content. " * 20).strip()
    rss_response.return_value = rss_article_response(title=title, url=url, body=body)

    article_ids = await acquisition_service.execute(source_id)

    articles = await load_completed_articles(system_database)
    assert len(articles) == 1
    article = articles[0]
    assert article_ids == [article["id"]]
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
    system_database, acquisition_service, source_id, rss_response
):
    """本文不足の記事を未完成記事として保存し、対応する補完依頼イベントを記録する。"""
    title = "Incomplete article"
    url = "https://venturebeat.com/acquisition-incomplete"
    rss_response.return_value = rss_article_response(title=title, url=url, body=None)

    article_ids = await acquisition_service.execute(source_id)

    assert article_ids == []
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
