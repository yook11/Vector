from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        PrimaryKeyConstraint("event_id", name="pk_outbox_events"),
        CheckConstraint("schema_version >= 1", name="ck_outbox_events_schema_version"),
        CheckConstraint("attempt_count >= 0", name="ck_outbox_events_attempt_count"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_outbox_events_payload_object"
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (leased_until IS NULL)",
            name="ck_outbox_events_lease_pair",
        ),
        CheckConstraint(
            "(delivery_stopped_at IS NULL) = (delivery_stop_reason IS NULL)",
            name="ck_outbox_events_delivery_stop_pair",
        ),
        CheckConstraint(
            "delivery_stop_reason IS NULL OR delivery_stop_reason ~ '[^[:space:]]'",
            name="ck_outbox_events_delivery_stop_reason",
        ),
        CheckConstraint(
            "published_at IS NULL OR delivery_stopped_at IS NULL",
            name="ck_outbox_events_delivery_outcome",
        ),
        Index(
            "ix_outbox_events_pending",
            "next_attempt_at",
            "event_id",
            postgresql_where=text(
                "published_at IS NULL AND delivery_stopped_at IS NULL"
            ),
        ),
    )

    event_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # SQSへの送信成功であり、後続処理の完了は表さない。
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # SDK内部のリトライを含めず、配信担当による送信試行を数える。
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_token: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 一時的な失敗では設定せず、自動配信を打ち切った時刻を保持する。
    delivery_stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # 理由コードのみ保存し、エラー詳細はevent_idで紐付くログに残す。
    delivery_stop_reason: Mapped[str | None] = mapped_column(Text)
