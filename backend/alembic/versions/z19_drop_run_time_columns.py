"""使われなくなったagent_runsの開始・終端時刻列を削除する。

Revision ID: z19_drop_run_time_columns
Revises: z18_agent_run_deadline
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "z19_drop_run_time_columns"
down_revision: str | None = "z18_agent_run_deadline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_KIND = "contract"


def _set_timeouts() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.execute("SET statement_timeout = '5s';")


def _reset_timeouts() -> None:
    op.execute("RESET statement_timeout")
    op.execute("RESET lock_timeout")


def upgrade() -> None:
    _set_timeouts()
    op.drop_column("agent_runs", "started_at")
    op.drop_column("agent_runs", "completed_at")
    _reset_timeouts()


def downgrade() -> None:
    # 列は復元できるが、削除前の開始・終端時刻の値は復元できない。
    _set_timeouts()
    op.add_column(
        "agent_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "agent_runs",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    _reset_timeouts()
