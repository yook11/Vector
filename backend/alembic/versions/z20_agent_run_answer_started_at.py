"""回答生成工程の開始時刻をagent_runsへ追加する。

Revision ID: z20_agent_run_answer_started_at
Revises: z19_drop_run_time_columns
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "z20_agent_run_answer_started_at"
down_revision: str | None = "z19_drop_run_time_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_KIND = "expand"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.add_column(
        "agent_runs",
        sa.Column(
            "answer_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # 列は削除できるが、記録済みの回答生成開始時刻は復元できない。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.drop_column("agent_runs", "answer_started_at")
