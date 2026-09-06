from __future__ import annotations

import asyncio

import asyncpg
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.lambda_handlers.outbox_relay import handler
from app.models.outbox_event import OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def relay_environment(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("DB_IAM_AUTH", "false")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    for stage in ("COMPLETION", "CURATION", "ASSESSMENT", "EMBEDDING"):
        monkeypatch.setenv(
            f"SQS_ARTICLE_{stage}_QUEUE_URL", f"https://sqs.invalid/{stage}"
        )


async def test_repeated_invocations_close_connections_and_preserve_outbox(
    relay_environment: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """実DBへ繰り返し接続しても配信状態を変えず、接続を残さない。"""
    async with session_factory() as writer:
        event = OutboxEvent(
            event_type="article.assessed_in_scope",
            payload={"curation_id": 123, "analyzed_article_id": 456},
        )
        writer.add(event)
        await writer.commit()

    async with session_factory() as reader:
        before = (await reader.execute(select(OutboxEvent.__table__))).mappings().all()

    for _ in range(2):
        result = await asyncio.to_thread(handler, {}, None)
        assert result == {"check": "database_connectivity", "status": "ok"}

    async with session_factory() as reader:
        after = (await reader.execute(select(OutboxEvent.__table__))).mappings().all()
        connections = await reader.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND application_name = 'vector-outbox-relay'"
            )
        )
    assert after == before
    assert connections == 0


async def test_database_failure_propagates_and_next_invocation_can_connect(
    relay_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
) -> None:
    """接続失敗を成功扱いせず、次の呼び出しで接続し直せる。"""
    missing_database = make_url(test_database_url).set(
        database="outbox_relay_test_database_does_not_exist"
    )
    monkeypatch.setenv(
        "DATABASE_URL", missing_database.render_as_string(hide_password=False)
    )
    with pytest.raises(asyncpg.InvalidCatalogNameError):
        await asyncio.to_thread(handler, {}, None)

    monkeypatch.setenv("DATABASE_URL", test_database_url)
    assert await asyncio.to_thread(handler, {}, None) == {
        "check": "database_connectivity",
        "status": "ok",
    }
