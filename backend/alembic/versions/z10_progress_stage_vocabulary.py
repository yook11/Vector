"""move agent run progress_stage vocabulary to the six answering stages

Revision ID: z10_progress_stage_vocabulary
Revises: y5_agent_run_policy_blocked
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "z10_progress_stage_vocabulary"
down_revision: str | None = "y5_agent_run_policy_blocked"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_KIND = "contract"

_CONSTRAINT = "ck_agent_runs_progress_stage"
_TABLE = "agent_runs"

_STAGE_CHECK_WITH_SIX_STAGES = (
    "progress_stage IN ("
    "'safety_check', 'context_resolution', 'planning', "
    "'evidence_collection', 'evidence_review', 'answering'"
    ")"
)
_STAGE_CHECK_WITH_THREE_STAGES = (
    "progress_stage IN ('planning', 'retrieving', 'synthesizing')"
)


def _set_timeouts() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.execute("SET statement_timeout = '5s';")


def _reset_timeouts() -> None:
    op.execute("RESET statement_timeout")
    op.execute("RESET lock_timeout")


def upgrade() -> None:
    _set_timeouts()
    # 写像の途中行が新旧どちらの CHECK にも掛からないよう、制約を外してから変換する。
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.execute(
        "UPDATE agent_runs SET progress_stage = 'evidence_collection' "
        "WHERE progress_stage = 'retrieving'"
    )
    op.execute(
        "UPDATE agent_runs SET progress_stage = 'answering' "
        "WHERE progress_stage = 'synthesizing'"
    )
    op.create_check_constraint(_CONSTRAINT, _TABLE, _STAGE_CHECK_WITH_SIX_STAGES)
    _reset_timeouts()


def downgrade() -> None:
    _set_timeouts()
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.execute(
        "UPDATE agent_runs SET progress_stage = 'retrieving' "
        "WHERE progress_stage IN ('evidence_collection', 'evidence_review')"
    )
    op.execute(
        "UPDATE agent_runs SET progress_stage = 'synthesizing' "
        "WHERE progress_stage = 'answering'"
    )
    # 旧語彙に対応する工程が無いため、この 2 値だけは値を捨てて NULL へ落とす。
    op.execute(
        "UPDATE agent_runs SET progress_stage = NULL "
        "WHERE progress_stage IN ('safety_check', 'context_resolution')"
    )
    op.create_check_constraint(_CONSTRAINT, _TABLE, _STAGE_CHECK_WITH_THREE_STAGES)
    _reset_timeouts()
