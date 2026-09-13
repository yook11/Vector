"""Curationの配送選択と失敗処理を、実入口から実DBまで接続する。"""

import asyncio
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ReadTimeoutError

from app.collection.article_acquisition.events import ArticleAcquired
from app.collection.article_completion.events import ArticleCompletedToAnalyzable
from app.collection.events import AnalyzableArticleCreated
from app.lambda_handlers.outbox_relay import curation_handler
from tests.outbox.curation_runtime import configure_curation_relay
from tests.outbox.helpers import PAST, insert_event, read_event

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def relay_runtime(monkeypatch, test_database_url):
    return configure_curation_relay(monkeypatch, test_database_url)


async def test_only_valid_new_event_is_delivered_and_legacy_events_are_untouched(
    relay_runtime, session_factory
):
    """共通契約の正常分だけを配送し、不正分の停止と旧種別の保持を確定する。"""
    async with session_factory() as session:
        good = await insert_event(
            session,
            next_attempt_at=PAST,
            event_type=AnalyzableArticleCreated.EVENT_TYPE,
            payload={"analyzable_article_id": 1},
        )
        invalid = await insert_event(
            session,
            next_attempt_at=PAST,
            event_type=AnalyzableArticleCreated.EVENT_TYPE,
            payload={"analyzable_article_id": "private-input"},
        )
        legacy = [
            await insert_event(
                session, next_attempt_at=PAST, event_type=event_type, attempt_count=5
            )
            for event_type in (
                ArticleAcquired.EVENT_TYPE,
                ArticleCompletedToAnalyzable.EVENT_TYPE,
            )
        ]
        await session.commit()
        before = {event_id: await read_event(session, event_id) for event_id in legacy}

    await asyncio.to_thread(curation_handler, {}, None)

    async with session_factory() as session:
        saved = {
            event_id: await read_event(session, event_id)
            for event_id in [good, invalid, *legacy]
        }
    sent_ids = [
        entry["Id"] for batch in relay_runtime.batches for entry in batch["Entries"]
    ]
    assert sent_ids == [str(good)]
    assert saved[good]["published_at"] is not None
    assert saved[invalid]["published_at"] is None
    assert saved[invalid]["delivery_stopped_at"] is not None
    assert saved[invalid]["delivery_stop_reason"] == "non_retryable_failure"
    assert {event_id: saved[event_id] for event_id in legacy} == before


async def test_transport_failure_keeps_event_for_retry(relay_runtime, session_factory):
    """送信の通信失敗は配送済みにせず、既存の再試行予約を確定する。"""
    async with session_factory() as session:
        event_id = await insert_event(
            session,
            next_attempt_at=PAST,
            event_type=AnalyzableArticleCreated.EVENT_TYPE,
            payload={"analyzable_article_id": 1},
        )
        await session.commit()
    attempted_at = datetime.now(UTC)
    relay_runtime.error = ReadTimeoutError(endpoint_url="https://sqs.invalid")

    await asyncio.to_thread(curation_handler, {}, None)

    async with session_factory() as session:
        saved = await read_event(session, event_id)
    assert saved["published_at"] is None
    assert saved["delivery_stopped_at"] is None
    assert saved["attempt_count"] == 1
    assert saved["next_attempt_at"] > attempted_at
    assert saved["lease_token"] is None
