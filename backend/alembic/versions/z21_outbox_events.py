"""ポーリング配信用のOutboxテーブルを追加

Revision ID: z21_outbox_events
Revises: z20_agent_run_answer_started_at
Create Date: 2026-09-06 09:31:18.672050

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from alembic import op

revision: str = "z21_outbox_events"
down_revision: str | None = "z20_agent_run_answer_started_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MIGRATION_KIND = "expand"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.create_table(
        "outbox_events",
        sa.Column(
            "event_id",
            PgUUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("lease_token", PgUUID(as_uuid=True), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_stop_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "delivery_stop_reason IS NULL OR delivery_stop_reason ~ '[^[:space:]]'",
            name="ck_outbox_events_delivery_stop_reason",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_outbox_events_payload_object"
        ),
        sa.CheckConstraint(
            "(delivery_stopped_at IS NULL) = (delivery_stop_reason IS NULL)",
            name="ck_outbox_events_delivery_stop_pair",
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL) = (leased_until IS NULL)",
            name="ck_outbox_events_lease_pair",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_outbox_events_attempt_count"),
        sa.CheckConstraint(
            "published_at IS NULL OR delivery_stopped_at IS NULL",
            name="ck_outbox_events_delivery_outcome",
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_outbox_events_schema_version"
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_outbox_events"),
        sa.Index(
            "ix_outbox_events_pending",
            "next_attempt_at",
            "event_id",
            postgresql_where=sa.text(
                "published_at IS NULL AND delivery_stopped_at IS NULL"
            ),
        ),
    )


def downgrade() -> None:
    # テーブルは削除できるが、保存済みイベントは復元できない。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.drop_index(
        "ix_outbox_events_pending",
        table_name="outbox_events",
        postgresql_where=sa.text(
            "published_at IS NULL AND delivery_stopped_at IS NULL"
        ),
    )
    op.drop_table("outbox_events")
