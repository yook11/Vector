"""SQS入口から実Consumer・Collect権限の確定保存と配送応答を確認する。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from app.lambda_handlers import article_fetch_lifecycle
from app.lambda_handlers.completion import composition
from app.lambda_handlers.completion.settings import CompletionConsumerSettings
from local_tests.completion.support import (
    article_response,
    seed_completed,
    seed_pending,
    stored_completion,
)
from tests.iam_fixtures import inject_test_db_signer


def message(name, target, *, source_id=None):
    return {
        "messageId": name,
        "receiptHandle": "receipt-" + name,
        "body": json.dumps(
            {
                "event_id": str(uuid4()),
                "event_type": "article.incomplete_recorded",
                "schema_version": 1,
                "occurred_at": "2026-09-14T00:00:00Z",
                "payload": {
                    "source_id": target.source_id if source_id is None else source_id,
                    "incomplete_article_id": target.id,
                },
            }
        ),
    }


@pytest.fixture
def delivery_runtime(system_database, monkeypatch):
    module = import_module("app.lambda_handlers.completion.handler")
    settings = CompletionConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url=inject_test_db_signer(
            monkeypatch,
            system_database.url("vector_collect", sqlalchemy=True),
            resources_module=article_fetch_lifecycle,
        ),
        db_iam_auth=True,
        sqs_article_completion_queue_url="https://sqs.ap-northeast-1.amazonaws.com/123456789012/completion",
    )
    monkeypatch.setattr(module, "CompletionConsumerSettings", lambda: settings)
    sqs = Mock(spec_set=["close", "change_message_visibility"])
    session = Mock(spec_set=["create_client"])
    session.create_client.return_value = sqs
    monkeypatch.setattr(composition, "Session", Mock(return_value=session))
    state = SimpleNamespace(
        handler=module.handler,
        context=SimpleNamespace(
            get_remaining_time_in_millis=Mock(return_value=600_000)
        ),
        sqs=sqs,
        settings=settings,
        now=datetime.now(UTC).replace(microsecond=0),
    )
    clock = Mock(wraps=datetime)
    clock.now.side_effect = lambda tz: state.now.astimezone(tz)
    monkeypatch.setattr(module, "datetime", clock)
    return state


@pytest.mark.asyncio
async def test_failed_messages_are_returned_and_later_article_completes(
    system_database,
    delivery_runtime,
    page_response,
):
    """再試行と不正入力だけを失敗一覧へ返し、後続の記事は完成する。"""
    retry_article = await seed_pending(
        system_database,
        "https://example.com/rate-limited",
    )
    successful_article = await seed_pending(
        system_database,
        "https://example.com/successful",
    )

    page_response.side_effect = [
        httpx.Response(429, headers={"Retry-After": "120"}),
        article_response("Successfully completed article"),
    ]
    batch = {
        "Records": [
            message("retry", retry_article),
            {"messageId": "invalid", "body": "{invalid-json"},
            message("success", successful_article),
        ],
    }

    response = await asyncio.to_thread(
        delivery_runtime.handler, batch, delivery_runtime.context
    )

    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "retry"},
            {"itemIdentifier": "invalid"},
        ]
    }
    retry_state = await stored_completion(system_database, retry_article)
    assert retry_state.pending["status"] == "open"
    assert retry_state.articles == []

    success_state = await stored_completion(system_database, successful_article)
    assert success_state.pending is None
    assert len(success_state.articles) == 1


@pytest.mark.asyncio
async def test_committed_closed_article_is_acknowledged(
    system_database,
    delivery_runtime,
    page_response,
):
    """403でclosedを確定した記事は、再配信対象に含めない。"""
    article = await seed_pending(system_database, "https://example.com/forbidden")
    page_response.return_value = httpx.Response(403)

    response = await asyncio.to_thread(
        delivery_runtime.handler,
        {"Records": [message("forbidden", article)]},
        delivery_runtime.context,
    )

    assert response == {"batchItemFailures": []}
    stored = await stored_completion(system_database, article)
    assert stored.pending["status"] == "closed"
    assert stored.articles == []


@pytest.mark.asyncio
async def test_already_closed_article_is_acknowledged_without_changes(
    system_database,
    delivery_runtime,
):
    """closed済みの記事は、確定状態を変更せず受信完了にする。"""
    article = await seed_pending(
        system_database, "https://example.com/already-closed", status="closed"
    )
    before = await stored_completion(system_database, article)

    response = await asyncio.to_thread(
        delivery_runtime.handler,
        {"Records": [message("closed", article)]},
        delivery_runtime.context,
    )

    assert response == {"batchItemFailures": []}
    assert await stored_completion(system_database, article) == before


@pytest.mark.asyncio
async def test_existing_article_is_preserved_and_message_is_acknowledged(
    system_database,
    delivery_runtime,
):
    """同URLの完成記事があれば、それを維持して未完成行を削除し受信完了にする。"""
    article = await seed_pending(system_database, "https://example.com/existing")
    await seed_completed(system_database, article)
    before = await stored_completion(system_database, article)

    response = await asyncio.to_thread(
        delivery_runtime.handler,
        {"Records": [message("existing", article)]},
        delivery_runtime.context,
    )

    assert response == {"batchItemFailures": []}
    after = await stored_completion(system_database, article)
    assert after.pending is None
    assert after.articles == before.articles
    assert after.audits == before.audits
    assert after.outbox == before.outbox


@pytest.mark.asyncio
async def test_invalid_event_is_returned_without_changing_article(
    system_database,
    delivery_runtime,
):
    """必須項目のないイベントは、指定された記事を変更せず再配信対象にする。"""
    article = await seed_pending(system_database, "https://example.com/invalid-event")
    before = await stored_completion(system_database, article)
    batch = {
        "Records": [
            {
                "messageId": "invalid",
                "body": json.dumps({"payload": {"incomplete_article_id": article.id}}),
            },
        ],
    }

    response = await asyncio.to_thread(
        delivery_runtime.handler, batch, delivery_runtime.context
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}
    assert await stored_completion(system_database, article) == before


@pytest.mark.asyncio
async def test_redelivery_completes_previously_retryable_article(
    system_database,
    delivery_runtime,
    page_response,
):
    """再試行対象と返した同じメッセージを再受信すると完成まで進められる。"""
    target = await seed_pending(system_database, "https://example.com/redelivery")
    batch = {"Records": [message("retry", target)]}
    page_response.return_value = httpx.Response(429, headers={"Retry-After": "120"})
    assert await asyncio.to_thread(
        delivery_runtime.handler, batch, delivery_runtime.context
    ) == {"batchItemFailures": [{"itemIdentifier": "retry"}]}
    before = await stored_completion(system_database, target)
    assert before.pending["status"] == "open"
    assert before.articles == before.outbox == []

    page_response.return_value = article_response("Recovered delivery")
    assert await asyncio.to_thread(
        delivery_runtime.handler, batch, delivery_runtime.context
    ) == {"batchItemFailures": []}
    after = await stored_completion(system_database, target)
    assert after.pending is None
    assert len(after.articles) == len(after.successes) == len(after.outbox) == 1


@pytest.mark.asyncio
async def test_failed_close_commit_is_reported_for_redelivery(
    system_database,
    delivery_runtime,
    page_response,
    control_commit,
):
    """closedの実DB確定失敗では未完成行を残し、受信完了にしない。"""
    target = await seed_pending(system_database, "https://example.com/close-failure")
    page_response.return_value = httpx.Response(403)
    with control_commit(target, phase="closed", fail=True) as fault:
        response = await asyncio.to_thread(
            delivery_runtime.handler,
            {"Records": [message("failed", target)]},
            delivery_runtime.context,
        )
    assert fault.error is not None
    assert response == {"batchItemFailures": [{"itemIdentifier": "failed"}]}
    stored = await stored_completion(system_database, target)
    assert stored.pending["status"] == "open"
    assert stored.articles == stored.successes == stored.outbox == []


@pytest.mark.asyncio
async def test_completed_message_redelivery_preserves_saved_article(
    system_database,
    delivery_runtime,
    page_response,
):
    """完成後の同じ配送を受信完了にし、保存済み本文とイベントを維持する。"""
    target = await seed_pending(system_database, "https://example.com/completed-again")
    batch = {"Records": [message("completed", target)]}
    assert await asyncio.to_thread(
        delivery_runtime.handler, batch, delivery_runtime.context
    ) == {"batchItemFailures": []}
    before = await stored_completion(system_database, target)
    assert len(before.articles) == 1
    page_response.return_value = article_response("Must not replace saved article")
    assert await asyncio.to_thread(
        delivery_runtime.handler, batch, delivery_runtime.context
    ) == {"batchItemFailures": []}
    assert await stored_completion(system_database, target) == before


@pytest.mark.asyncio
async def test_database_source_wins_over_event_source(
    system_database,
    delivery_runtime,
):
    """イベントのsource_idが異なってもDBのソースと記事内容で確定する。"""
    target = await seed_pending(system_database, "https://example.com/db-source")
    response = await asyncio.to_thread(
        delivery_runtime.handler,
        {"Records": [message("source", target, source_id=target.source_id + 1000000)]},
        delivery_runtime.context,
    )
    assert response == {"batchItemFailures": []}
    stored = await stored_completion(system_database, target)
    assert stored.pending is None
    assert len(stored.articles) == 1
    assert stored.articles[0]["source_id"] == target.source_id
    assert stored.articles[0]["original_title"] == target.title


@pytest.mark.asyncio
async def test_http_retry_time_reaches_visibility_and_partial_response(
    system_database,
    delivery_runtime,
    page_response,
):
    """実Consumerの429待機時刻が可視性変更へ届いても再配信対象を維持する。"""
    target = await seed_pending(system_database, "https://example.com/wait")
    retry_time = delivery_runtime.now + timedelta(minutes=10)
    page_response.return_value = httpx.Response(
        429, headers={"Retry-After": format_datetime(retry_time, usegmt=True)}
    )
    response = await asyncio.to_thread(
        delivery_runtime.handler,
        {"Records": [message("wait", target)]},
        delivery_runtime.context,
    )
    delivery_runtime.sqs.change_message_visibility.assert_called_once_with(
        QueueUrl=delivery_runtime.settings.sqs_article_completion_queue_url,
        ReceiptHandle="receipt-wait",
        VisibilityTimeout=600,
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "wait"}]}
    stored = await stored_completion(system_database, target)
    assert stored.pending["status"] == "open"
    assert stored.articles == stored.outbox == []


@pytest.mark.asyncio
async def test_visibility_failure_keeps_article_open_for_redelivery(
    system_database,
    delivery_runtime,
    page_response,
):
    """可視性変更の障害で実DBの記事をclosed化せず再配信対象にする。"""
    target = await seed_pending(system_database, "https://example.com/wait-failure")
    page_response.return_value = httpx.Response(429, headers={"Retry-After": "120"})
    delivery_runtime.sqs.change_message_visibility.side_effect = RuntimeError(
        "private-sqs"
    )
    response = await asyncio.to_thread(
        delivery_runtime.handler,
        {"Records": [message("failed-wait", target)]},
        delivery_runtime.context,
    )
    delivery_runtime.sqs.change_message_visibility.assert_called_once()
    assert response == {"batchItemFailures": [{"itemIdentifier": "failed-wait"}]}
    stored = await stored_completion(system_database, target)
    assert stored.pending["status"] == "open"
    assert stored.articles == stored.successes == stored.outbox == []


@pytest.mark.asyncio
async def test_insufficient_time_preserves_unstarted_article(
    system_database,
    delivery_runtime,
):
    """先行記事の確定後に時間不足なら、未着手記事と監査を変更しない。"""
    first = await seed_pending(system_database, "https://example.com/started")
    unstarted = await seed_pending(system_database, "https://example.com/unstarted")
    before = await stored_completion(system_database, unstarted)
    delivery_runtime.context.get_remaining_time_in_millis.side_effect = [60_000, 59_999]
    response = await asyncio.to_thread(
        delivery_runtime.handler,
        {"Records": [message("started", first), message("unstarted", unstarted)]},
        delivery_runtime.context,
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "unstarted"}]}
    assert await stored_completion(system_database, unstarted) == before
    assert (await stored_completion(system_database, first)).pending is None


@pytest.mark.asyncio
async def test_sqs_cleanup_failure_preserves_delivery_response(
    system_database,
    delivery_runtime,
    page_response,
):
    """待機設定後のクライアント解放障害でもDB結果と再配信応答を保つ。"""
    target = await seed_pending(system_database, "https://example.com/wait-cleanup")
    page_response.return_value = httpx.Response(429, headers={"Retry-After": "120"})
    delivery_runtime.sqs.close.side_effect = RuntimeError("private-close")
    response = await asyncio.to_thread(
        delivery_runtime.handler,
        {"Records": [message("cleanup", target)]},
        delivery_runtime.context,
    )
    delivery_runtime.sqs.change_message_visibility.assert_called_once()
    delivery_runtime.sqs.close.assert_called_once()
    assert response == {"batchItemFailures": [{"itemIdentifier": "cleanup"}]}
    assert (await stored_completion(system_database, target)).pending[
        "status"
    ] == "open"
