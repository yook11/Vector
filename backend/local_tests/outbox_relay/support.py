"""Relay配送テストのイベント準備と実行入口。"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from app.analysis.assessment.events import ArticleAssessedInScope
from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.relay import OutboxRelay
from app.outbox.publishing.errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
)
from tests.outbox.helpers import PAST, insert_event, read_event

EVENT_TYPE = ArticleAssessedInScope.EVENT_TYPE


@dataclass(frozen=True)
class SeededEvent:
    event_id: UUID
    before: dict


@dataclass
class ClaimHold:
    claimed: asyncio.Event = field(default_factory=asyncio.Event)
    allow: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_claimed(self):
        await asyncio.wait_for(self.claimed.wait(), 10)

    def release(self):
        self.allow.set()


def configuration_error():
    return PublishConfigurationError(
        reason=PublishConfigurationReason.MISSING_CREDENTIALS
    )


async def seed_events(owner_sessions, count=1, **fields):
    async with owner_sessions() as session:
        ids = [
            await insert_event(
                session,
                next_attempt_at=PAST,
                event_type=fields.get("event_type", EVENT_TYPE),
                occurred_at=PAST + timedelta(seconds=i),
                payload={"curation_id": i + 1, "analyzed_article_id": i + 101},
                attempt_count=fields.get("attempt_count", 0),
            )
            for i in range(count)
        ]
        await session.commit()
        return tuple(
            [
                SeededEvent(event_id, await read_event(session, event_id))
                for event_id in ids
            ]
        )


async def seed_event(owner_sessions, **fields):
    (event,) = await seed_events(owner_sessions, **fields)
    return event


async def read_saved(owner_sessions, event):
    async with owner_sessions() as session:
        return await read_event(session, event.event_id)


def make_relay(sessions, publisher, *, event_type=EVENT_TYPE):
    return OutboxRelay(
        sessions,
        publisher,
        OutboxDeliveryFailureHandler(sessions, jitter=lambda: 0.5),
        event_type=event_type,
    )
