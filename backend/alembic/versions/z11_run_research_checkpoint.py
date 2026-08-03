"""add research_checkpoint JSONB column to agent_runs.

completed run が実行した外部検索の決定的な記録(ResearchCheckpoint)を
Runと1対1で保持する。既存行はNULLのまま、外部検索を実行しcompletedになった
Runのみ実値を持つ。

Revision ID: z11_run_research_checkpoint
Revises: z10_progress_stage_vocabulary
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "z11_run_research_checkpoint"
down_revision: str | None = "z10_progress_stage_vocabulary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: nullable column の追加のみ
# (破壊系なし、op.execute は SET lock_timeout のみ)。
MIGRATION_KIND = "expand"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column(
        "agent_runs",
        sa.Column("research_checkpoint", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "research_checkpoint")
