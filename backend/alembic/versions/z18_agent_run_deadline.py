"""add fixed deadline to agent runs.

本番はmigrationを旧API停止前に適用するため、旧APIから新規runが作られない
無通信時間帯に適用する。通常トラフィック下ではexpand/contractへ分割すること。

Revision ID: z18_agent_run_deadline
Revises: z17_drop_progress_stage
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "z18_agent_run_deadline"
down_revision: str | None = "z17_drop_progress_stage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_KIND = "contract"

_TABLE = "agent_runs"
_STATUS_CONSTRAINT = "ck_agent_runs_status"
_STATUS_CHECK_WITH_DEADLINE = (
    "status IN ("
    "'queued', 'running', 'completed', 'policy_blocked', "
    "'deadline_exceeded', 'failed'"
    ")"
)
_STATUS_CHECK_WITHOUT_DEADLINE = (
    "status IN ('queued', 'running', 'completed', 'policy_blocked', 'failed')"
)
_OFFLINE_DOWNGRADE_GUARD = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_runs WHERE status = 'deadline_exceeded'
    ) THEN
        RAISE EXCEPTION 'cannot downgrade while deadline_exceeded agent_runs exist';
    END IF;
END
$$;
"""


def _set_timeouts() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.execute("SET statement_timeout = '5s';")


def _reset_timeouts() -> None:
    op.execute("RESET statement_timeout")
    op.execute("RESET lock_timeout")


def upgrade() -> None:
    _set_timeouts()
    op.add_column(
        _TABLE,
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE agent_runs "
        "SET deadline_at = created_at + INTERVAL '60 seconds' "
        "WHERE deadline_at IS NULL"
    )
    op.alter_column(
        _TABLE,
        "deadline_at",
        nullable=False,
    )
    op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _STATUS_CONSTRAINT,
        _TABLE,
        _STATUS_CHECK_WITH_DEADLINE,
    )
    _reset_timeouts()


def downgrade() -> None:
    _set_timeouts()
    if op.get_context().as_sql:
        op.execute(_OFFLINE_DOWNGRADE_GUARD)
    else:
        _refuse_lossy_downgrade()
    op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _STATUS_CONSTRAINT,
        _TABLE,
        _STATUS_CHECK_WITHOUT_DEADLINE,
    )
    op.drop_column(_TABLE, "deadline_at")
    _reset_timeouts()


def _refuse_lossy_downgrade() -> None:
    deadline_exceeded_exists = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM agent_runs WHERE status = 'deadline_exceeded'"
                ")"
            )
        )
        .scalar_one()
    )
    if deadline_exceeded_exists:
        raise RuntimeError("cannot downgrade while deadline_exceeded agent_runs exist")
