"""grant agent history tables to vector_app

agent history tables は y1 で追加されたが、既存 Neon DB では vector_app への
default privileges が効かず runtime が権限不足になる可能性がある。
agent runtime の DB 契約として、対象 4 table と sequence 権限を明示 GRANT する。

Revision ID: y2_agent_history_grants
Revises: y1_agent_history
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "y2_agent_history_grants"
down_revision: str | None = "y1_agent_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: runtime role の DB 権限契約変更のため手動適用対象。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vector_app') THEN
            RAISE EXCEPTION
              'role vector_app not found. '
              'Run infra/db/init/01_create_app_users.sh '
              'or create the role manually before running this migration.';
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON TABLE agent_threads, agent_messages, agent_message_sources, agent_runs
        TO vector_app
        """
    )
    op.execute(
        """
        GRANT USAGE, SELECT, UPDATE
        ON ALL SEQUENCES IN SCHEMA public
        TO vector_app
        """
    )


def downgrade() -> None:
    # n3_grant_app_db_users が vector_app の public DML 契約を所有しているため、
    # y2 の downgrade で REVOKE すると既存契約まで剥がす。権限削除はしない。
    pass
