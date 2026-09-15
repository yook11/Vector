"""Outbox配信専用ロールに配信状態の参照・更新を許可する。

Revision ID: z23_grant_outbox_relay
Revises: z22_grant_collect_outbox
"""

from collections.abc import Sequence

from alembic import op

revision: str = "z23_grant_outbox_relay"
down_revision: str | None = "z22_grant_collect_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute("GRANT USAGE ON SCHEMA public TO vector_outbox_relay")
    op.execute(
        "GRANT SELECT (event_id, event_type, schema_version, payload, occurred_at, "
        "published_at, next_attempt_at, attempt_count, lease_token, leased_until, "
        "delivery_stopped_at) ON public.outbox_events TO vector_outbox_relay"
    )
    op.execute(
        "GRANT UPDATE (lease_token, leased_until, attempt_count, published_at, "
        "next_attempt_at, delivery_stopped_at, delivery_stop_reason) "
        "ON public.outbox_events TO vector_outbox_relay"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(
        "REVOKE UPDATE (lease_token, leased_until, attempt_count, published_at, "
        "next_attempt_at, delivery_stopped_at, delivery_stop_reason) "
        "ON public.outbox_events FROM vector_outbox_relay"
    )
    op.execute(
        "REVOKE SELECT (event_id, event_type, schema_version, payload, occurred_at, "
        "published_at, next_attempt_at, attempt_count, lease_token, leased_until, "
        "delivery_stopped_at) ON public.outbox_events FROM vector_outbox_relay"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_outbox_relay")
