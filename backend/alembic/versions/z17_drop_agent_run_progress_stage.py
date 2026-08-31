"""drop unused agent_runs.progress_stage column.

工程の表示正本は Redis Stream であり、worker は列を書かない。
列の既存値は破棄する。downgrade は空の nullable 列と CHECK を戻すだけで、
値は復元しない。

Revision ID: z17_drop_progress_stage
Revises: z16_thread_research_handoff
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "z17_drop_progress_stage"
down_revision: str | None = "z16_thread_research_handoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_KIND = "contract"

_CONSTRAINT = "ck_agent_runs_progress_stage"
_TABLE = "agent_runs"
_COLUMN = "progress_stage"
_STAGE_CHECK = (
    "progress_stage IN ("
    "'safety_check', 'context_resolution', 'planning', "
    "'evidence_collection', 'evidence_review', 'answering'"
    ")"
)


def _set_timeouts() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.execute("SET statement_timeout = '5s';")


def _reset_timeouts() -> None:
    op.execute("RESET statement_timeout")
    op.execute("RESET lock_timeout")


def upgrade() -> None:
    _set_timeouts()
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
    _reset_timeouts()


def downgrade() -> None:
    # schema は戻せるが、drop 前の progress_stage 値は再現しない。
    _set_timeouts()
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(_CONSTRAINT, _TABLE, _STAGE_CHECK)
    _reset_timeouts()
