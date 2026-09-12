from __future__ import annotations

import asyncio
from importlib import import_module
from unittest.mock import Mock

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.lambda_handlers.outbox_relay import handler
from app.models.outbox_event import OutboxEvent
from app.outbox.publishing.publisher import BatchPublishResult, PublishSucceeded
from tests.iam_fixtures import inject_test_db_signer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def relay_environment(monkeypatch: pytest.MonkeyPatch, test_database_url: str) -> str:
    database_url = inject_test_db_signer(monkeypatch, test_database_url)
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DB_IAM_AUTH", "true")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv(
        "SQS_ARTICLE_EMBEDDING_QUEUE_URL", "https://sqs.invalid/EMBEDDING"
    )
    return database_url


@pytest.fixture
def publisher(monkeypatch):
    publisher = Mock()
    publisher.publish_batch.side_effect = lambda envelopes: BatchPublishResult(
        results=tuple(PublishSucceeded(e.event_id) for e in envelopes)
    )
    monkeypatch.setattr(
        import_module("app.lambda_handlers.outbox_relay.execution"),
        "RoutedEventPublisher",
        Mock(return_value=publisher),
    )
    return publisher


async def test_repeated_invocations_record_delivery_without_resending(
    relay_environment: str,
    session_factory: async_sessionmaker[AsyncSession],
    publisher,
) -> None:
    """入口から配信を確定し、次回起動では配信済みイベントを再送しない。"""
    async with session_factory() as writer:
        event = OutboxEvent(
            event_type="article.assessed_in_scope",
            payload={"curation_id": 123, "analyzed_article_id": 456},
        )
        writer.add(event)
        await writer.commit()
        event_id = event.event_id

    for _ in range(2):
        await asyncio.to_thread(handler, {}, None)

    async with session_factory() as reader:
        saved = await reader.get(OutboxEvent, event_id)
        assert saved is not None
        assert saved.published_at is not None
    publisher.publish_batch.assert_called_once()
    assert [e.event_id for e in publisher.publish_batch.call_args.args[0]] == [event_id]


async def test_repeated_invocations_release_database_connections(
    relay_environment: str,
    session_factory: async_sessionmaker[AsyncSession],
    publisher,
) -> None:
    """繰り返しの起動が終わった後、relayのDB接続を残さない。"""
    for _ in range(2):
        await asyncio.to_thread(handler, {}, None)

    # DB側の終了反映を待ち、観測ごとにトランザクションを終了する。
    for attempt in range(6):
        async with session_factory() as reader:
            connections = await reader.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND application_name = 'vector-outbox-relay'"
                )
            )
        if connections == 0:
            break
        if attempt < 5:
            await asyncio.sleep(0.05)
    assert connections == 0


async def test_database_failure_propagates_and_next_invocation_can_connect(
    relay_environment: str,
    monkeypatch: pytest.MonkeyPatch,
    publisher,
) -> None:
    """接続失敗を成功扱いせず、次の呼び出しで接続し直せる。"""
    missing_database = make_url(relay_environment).set(
        database="outbox_relay_test_database_does_not_exist"
    )
    monkeypatch.setenv(
        "DATABASE_URL", missing_database.render_as_string(hide_password=False)
    )
    with pytest.raises(asyncpg.InvalidCatalogNameError):
        await asyncio.to_thread(handler, {}, None)

    monkeypatch.setenv("DATABASE_URL", relay_environment)
    assert await asyncio.to_thread(handler, {}, None) == {"status": "completed"}
