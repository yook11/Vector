"""取得記事の充足状態に応じた保存先と後続イベントを確認する。"""

from unittest.mock import Mock

import httpx
import pytest
from structlog.testing import capture_logs

from local_tests.acquisition.support import (
    load_acquisition_failures,
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
    with capture_logs() as logs:
        assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    rss_response.assert_not_awaited()
    assert any(
        log["event"] == "acquisition_message_processed" and log["result"] == "inactive"
        for log in logs
    )


async def test_deleted_source_is_acknowledged_without_fetch(
    system_database, invoke_acquisition, source_id, rss_response
):
    """開始前に削除されたソースは再配信せず取得を終える。"""
    async with system_database.connect("vector") as db:
        await db.execute("DELETE FROM news_sources WHERE id=$1", source_id)
    with capture_logs() as logs:
        assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    rss_response.assert_not_awaited()
    assert any(
        log["event"] == "acquisition_message_processed" and log["result"] == "missing"
        for log in logs
    )


async def test_inactive_source_with_invalid_name_is_not_resolved(
    system_database, invoke_acquisition, source_id, rss_response
):
    """無効ソースは名前が不正でも取得不要として正常終了する。"""
    async with system_database.connect("vector") as db:
        await db.execute(
            "UPDATE news_sources SET name='!!!', is_active=false WHERE id=$1",
            source_id,
        )
    with capture_logs() as logs:
        assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    rss_response.assert_not_awaited()
    assert any(
        log["event"] == "acquisition_message_processed" and log["result"] == "inactive"
        for log in logs
    )


async def test_active_source_with_invalid_name_is_failed_without_fetch(
    system_database, invoke_acquisition, source_id, rss_response
):
    """有効ソースの不正名はDB読み取り障害と区別し、登録不正として再配送する。"""
    async with system_database.connect("vector") as db:
        await db.execute("UPDATE news_sources SET name='!!!' WHERE id=$1", source_id)
    with capture_logs() as logs:
        assert await invoke_acquisition(source_id) == {
            "batchItemFailures": [{"itemIdentifier": "acquisition-message"}]
        }
    rss_response.assert_not_awaited()
    assert any(
        log["event"] == "acquisition_message_processed"
        and log["code"] == "source_not_registered"
        for log in logs
    )


async def test_received_request_does_not_reselect_source_by_cadence(
    system_database, invoke_acquisition, source_id, rss_response
):
    """受信済み依頼はソースの頻度と異なっても、現在有効なら取得する。"""
    rss_response.return_value = rss_article_response(
        title="Received low cadence request",
        url="https://venturebeat.com/received-cadence",
        body="Article content. " * 40,
    )
    assert await invoke_acquisition(source_id, cadence="low") == {
        "batchItemFailures": []
    }
    rss_response.assert_awaited_once()
    assert len(await load_completed_articles(system_database)) == 1


async def test_source_lookup_releases_database_connection_before_http(
    system_database, invoke_acquisition, source_id, rss_response
):
    """取得前のソース確認で開いたDB接続をHTTP待機へ持ち越さない。"""
    connections_at_fetch = []

    async def respond(*args, **kwargs):
        async with system_database.connect("vector") as db:
            connections_at_fetch.append(
                await db.fetchval(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() "
                    "AND application_name='vector-acquisition-consumer'"
                )
            )
        return rss_article_response(
            title="Source lookup finished",
            url="https://venturebeat.com/source-lookup-finished",
            body="Article content. " * 40,
        )

    rss_response.side_effect = respond
    assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    assert connections_at_fetch == [0]


@pytest.mark.parametrize(
    ("fetch_outcome", "expected_code", "expected_http_status", "expected_retryability"),
    [
        pytest.param(
            httpx.ReadTimeout("private-fetch-detail"),
            "http_transport_error",
            None,
            "retryable",
            id="timeout",
        ),
        pytest.param(
            [httpx.Response(404)],
            "http_response_error",
            404,
            "non_retryable",
            id="not_found",
        ),
    ],
)
async def test_fetch_failure_is_redelivered_and_can_recover(
    system_database,
    invoke_acquisition,
    source_id,
    rss_response,
    fetch_outcome,
    expected_code,
    expected_http_status,
    expected_retryability,
):
    """取得の失敗を監査に残してSQSへ返し、次の配送で回復できる。"""
    rss_response.side_effect = fetch_outcome
    assert await invoke_acquisition(source_id) == {
        "batchItemFailures": [{"itemIdentifier": "acquisition-message"}]
    }
    assert await load_stored_events(system_database) == []
    failures = await load_acquisition_failures(system_database, source_id)
    assert len(failures) == 1
    assert failures[0]["outcome_code"] == "rss_feed_errors"
    assert failures[0]["retryability"] == expected_retryability
    assert failures[0]["payload"]["source_name"] == "VentureBeat"
    assert failures[0]["payload"]["feed_failures"][0]["code"] == expected_code
    assert (
        failures[0]["payload"]["feed_failures"][0]["http_status"]
        == expected_http_status
    )
    rss_response.side_effect = None
    rss_response.return_value = rss_article_response(
        title="Recovered article",
        url="https://venturebeat.com/recovered",
        body="Recovered body. " * 40,
    )
    assert await invoke_acquisition(source_id) == {"batchItemFailures": []}
    assert len(await load_stored_events(system_database)) == 1


async def test_save_failure_rolls_back_articles_and_commits_failure_audit(
    system_database, invoke_acquisition, source_id, rss_response
):
    """保存失敗のトランザクションを取り消しても、別セッションの失敗監査は残る。"""
    rss_response.return_value = rss_article_response(
        title="Outbox write fails",
        url="https://venturebeat.com/outbox-write-fails",
        body="Article content. " * 40,
    )
    async with system_database.connect("vector") as db:
        await db.execute("REVOKE INSERT ON outbox_events FROM vector_collect")
    try:
        assert await invoke_acquisition(source_id) == {
            "batchItemFailures": [{"itemIdentifier": "acquisition-message"}]
        }
    finally:
        async with system_database.connect("vector") as db:
            await db.execute("GRANT INSERT ON outbox_events TO vector_collect")

    assert await load_completed_articles(system_database) == []
    assert await load_stored_events(system_database) == []
    failures = await load_acquisition_failures(system_database, source_id)
    assert len(failures) == 1
    assert failures[0]["error_class"] == "app.db.errors.DatabaseUnexpectedError"
    assert failures[0]["payload"]["source_name"] == "VentureBeat"


async def test_audit_and_diagnostic_failures_preserve_original_fetch_failure(
    system_database, invoke_acquisition, source_id, rss_response, monkeypatch
):
    """監査保存と退避ログが失敗しても、元の取得例外で再配送を要求する。"""
    from app.collection.article_acquisition import failure_recording

    rss_response.side_effect = httpx.ReadTimeout("private-fetch-detail")
    audit_log = Mock()
    audit_log.exception.side_effect = RuntimeError("private-diagnostic-detail")
    monkeypatch.setattr(failure_recording, "logger", audit_log)
    async with system_database.connect("vector") as db:
        await db.execute("REVOKE INSERT ON pipeline_events FROM vector_collect")
    try:
        with capture_logs() as logs:
            response = await invoke_acquisition(source_id)
    finally:
        async with system_database.connect("vector") as db:
            await db.execute("GRANT INSERT ON pipeline_events TO vector_collect")

    audit_log.exception.assert_called_once()
    assert response == {
        "batchItemFailures": [{"itemIdentifier": "acquisition-message"}]
    }
    processed = [log for log in logs if log["event"] == "acquisition_message_processed"]
    assert len(processed) == 1
    assert processed[0]["error_class"] == (
        "app.collection.article_acquisition.errors.RssFeedErrors"
    )


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
