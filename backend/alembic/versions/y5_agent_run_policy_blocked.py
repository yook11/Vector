"""add policy blocked agent run status

Revision ID: y5_agent_run_policy_blocked
Revises: y4_agent_user_daily_quotas
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "y5_agent_run_policy_blocked"
down_revision: str | None = "y4_agent_user_daily_quotas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_KIND = "contract"

_STATUS_CHECK_WITH_POLICY_BLOCKED = (
    "status IN ('queued', 'running', 'completed', 'policy_blocked', 'failed')"
)
_STATUS_CHECK_WITHOUT_POLICY_BLOCKED = (
    "status IN ('queued', 'running', 'completed', 'failed')"
)
_OFFLINE_DOWNGRADE_GUARD = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_runs WHERE status = 'policy_blocked'
    ) THEN
        RAISE EXCEPTION 'cannot downgrade while policy_blocked agent_runs exist';
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
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        _STATUS_CHECK_WITH_POLICY_BLOCKED,
    )
    _reset_timeouts()


def downgrade() -> None:
    _set_timeouts()
    if op.get_context().as_sql:
        op.execute(_OFFLINE_DOWNGRADE_GUARD)
    else:
        _refuse_lossy_downgrade()
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        _STATUS_CHECK_WITHOUT_POLICY_BLOCKED,
    )
    _reset_timeouts()


def _refuse_lossy_downgrade() -> None:
    policy_blocked_exists = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM agent_runs WHERE status = 'policy_blocked'"
                ")"
            )
        )
        .scalar_one()
    )
    if policy_blocked_exists:
        raise RuntimeError("cannot downgrade while policy_blocked agent_runs exist")
