from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.outbox_event import OutboxEvent

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 6, tzinfo=UTC)


async def test_server_defaults_and_unique_event_ids(db_session: AsyncSession) -> None:
    rows = (
        (
            await db_session.execute(
                insert(OutboxEvent)
                .values(
                    [
                        {
                            "event_type": "article.curated",
                            "payload": {"curation_id": 1},
                        },
                        {
                            "event_type": "article.curated",
                            "payload": {"curation_id": 2},
                        },
                    ]
                )
                .returning(OutboxEvent)
            )
        )
        .scalars()
        .all()
    )
    assert len({row.event_id for row in rows}) == 2
    for row in rows:
        assert isinstance(row.event_id, UUID)
        assert row.schema_version == 1
        assert row.attempt_count == 0
        assert row.occurred_at.utcoffset().total_seconds() == 0
        assert row.next_attempt_at == row.occurred_at
        assert (
            row.published_at,
            row.lease_token,
            row.leased_until,
            row.delivery_stopped_at,
            row.delivery_stop_reason,
        ) == (None, None, None, None, None)


@pytest.mark.parametrize(
    "column",
    [
        "event_id",
        "event_type",
        "schema_version",
        "payload",
        "occurred_at",
        "next_attempt_at",
        "attempt_count",
    ],
)
async def test_required_columns_reject_sql_null(
    db_session: AsyncSession,
    column: str,
) -> None:
    values = {"event_type": "article.curated", "payload": {}}
    values[column] = null()
    with pytest.raises(IntegrityError, match="not-null constraint"):
        async with db_session.begin_nested():
            await db_session.execute(insert(OutboxEvent.__table__).values(values))


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"schema_version": 0}, "ck_outbox_events_schema_version"),
        ({"schema_version": -1}, "ck_outbox_events_schema_version"),
        ({"attempt_count": -1}, "ck_outbox_events_attempt_count"),
        ({"payload": []}, "ck_outbox_events_payload_object"),
        ({"payload": "text"}, "ck_outbox_events_payload_object"),
        ({"payload": 1}, "ck_outbox_events_payload_object"),
        ({"payload": True}, "ck_outbox_events_payload_object"),
        ({"payload": None}, "ck_outbox_events_payload_object"),
        ({"lease_token": uuid4()}, "ck_outbox_events_lease_pair"),
        ({"leased_until": NOW}, "ck_outbox_events_lease_pair"),
        ({"delivery_stopped_at": NOW}, "ck_outbox_events_delivery_stop_pair"),
        (
            {"delivery_stop_reason": "invalid_message"},
            "ck_outbox_events_delivery_stop_pair",
        ),
        (
            {"delivery_stopped_at": NOW, "delivery_stop_reason": ""},
            "ck_outbox_events_delivery_stop_reason",
        ),
        (
            {"delivery_stopped_at": NOW, "delivery_stop_reason": "   "},
            "ck_outbox_events_delivery_stop_reason",
        ),
        (
            {"delivery_stopped_at": NOW, "delivery_stop_reason": "\t\n\r"},
            "ck_outbox_events_delivery_stop_reason",
        ),
        (
            {
                "published_at": NOW,
                "delivery_stopped_at": NOW,
                "delivery_stop_reason": "invalid_message",
            },
            "ck_outbox_events_delivery_outcome",
        ),
    ],
)
async def test_invalid_delivery_states_are_rejected(
    db_session: AsyncSession,
    changes: dict,
    constraint: str,
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(OutboxEvent.__table__).values(
                    {"event_type": "article.curated", "payload": {}, **changes}
                )
            )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"published_at": NOW},
        {
            "lease_token": UUID("00000000-0000-4000-a000-000000000003"),
            "leased_until": NOW,
        },
        {"delivery_stopped_at": NOW, "delivery_stop_reason": "invalid_message"},
        {"schema_version": 2, "attempt_count": 3, "payload": {"curation_id": 123}},
    ],
)
async def test_valid_delivery_states_are_persisted(
    db_session: AsyncSession,
    changes: dict,
) -> None:
    row = (
        await db_session.execute(
            insert(OutboxEvent)
            .values({"event_type": "article.curated", "payload": {}, **changes})
            .returning(OutboxEvent)
        )
    ).scalar_one()
    assert {key: getattr(row, key) for key in changes} == changes
    assert row.event_type == "article.curated"


async def test_duplicate_event_id_is_rejected(db_session: AsyncSession) -> None:
    values = {"event_id": uuid4(), "event_type": "article.curated", "payload": {}}
    await db_session.execute(insert(OutboxEvent).values(values))
    with pytest.raises(IntegrityError, match="pk_outbox_events"):
        async with db_session.begin_nested():
            await db_session.execute(insert(OutboxEvent).values(values))


@pytest.mark.parametrize("commit", [True, False])
async def test_transaction_controls_event_visibility(
    session_factory: async_sessionmaker[AsyncSession],
    commit: bool,
) -> None:
    event_id = uuid4()
    async with session_factory() as writer:
        writer.add(
            OutboxEvent(event_id=event_id, event_type="article.curated", payload={})
        )
        await writer.flush()
        async with session_factory() as reader:
            assert await reader.get(OutboxEvent, event_id) is None
        if commit:
            await writer.commit()
        else:
            await writer.rollback()
    async with session_factory() as reader:
        assert (
            await reader.scalar(
                select(OutboxEvent.event_id).where(OutboxEvent.event_id == event_id)
            )
            is not None
        ) == commit
