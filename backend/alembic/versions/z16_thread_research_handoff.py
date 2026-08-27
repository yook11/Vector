"""add research_handoff JSONB column to agent_threads.

thread が積み上げた調査の申し送り(ResearchHandoff)を thread と1対1で保持する。
正本は常に最新の1本で、調査を行った Run が完了するたびに上書きする。
既存行は NULL のままで、次に調査を行った時点で実値を持つ。backfill しない。

Revision ID: z16_thread_research_handoff
Revises: z11_run_research_checkpoint
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "z16_thread_research_handoff"
down_revision: str | None = "z11_run_research_checkpoint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: nullable column の追加のみ
# (破壊系なし、op.execute は SET lock_timeout のみ)。
MIGRATION_KIND = "expand"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column(
        "agent_threads",
        sa.Column("research_handoff", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_threads", "research_handoff")
