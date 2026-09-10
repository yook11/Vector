"""取得ワーカーにOutbox追加と既定値の取得を許可する。

Revision ID: z22_grant_collect_outbox
Revises: z21_outbox_events
"""

from collections.abc import Sequence

from alembic import op

revision: str = "z22_grant_collect_outbox"
down_revision: str | None = "z21_outbox_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute("GRANT INSERT ON outbox_events TO vector_collect")
    # server defaultのRETURNINGに限定し、payloadや配信操作権限を渡さない。
    op.execute(
        "GRANT SELECT (event_id, schema_version, occurred_at, next_attempt_at, "
        "attempt_count) ON outbox_events TO vector_collect"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(
        "REVOKE SELECT (event_id, schema_version, occurred_at, next_attempt_at, "
        "attempt_count) ON outbox_events FROM vector_collect"
    )
    op.execute("REVOKE INSERT ON outbox_events FROM vector_collect")
