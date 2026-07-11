"""add monotonic attempt epoch to agent runs

Revision ID: y3_agent_runs_attempt_epoch
Revises: y2_agent_history_grants
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "y3_agent_runs_attempt_epoch"
down_revision: str | None = "y2_agent_history_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 既存行のbackfillとCHECK検証を伴うため、手動適用対象とする。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column(
        "agent_runs",
        sa.Column(
            "attempt_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(
        """
        UPDATE agent_runs
        SET attempt_epoch = 1
        WHERE started_at IS NOT NULL
        """
    )
    op.create_check_constraint(
        "ck_agent_runs_attempt_epoch_nonnegative",
        "agent_runs",
        "attempt_epoch >= 0",
    )


def downgrade() -> None:
    # downgradeで正確なepoch履歴は失われ、再upgrade時はstarted_atから0/1へ戻る。
    op.execute("SET lock_timeout = '5s';")
    op.drop_constraint(
        "ck_agent_runs_attempt_epoch_nonnegative",
        "agent_runs",
        type_="check",
    )
    op.drop_column("agent_runs", "attempt_epoch")
