"""add user daily agent request quota persistence

Revision ID: y4_agent_user_daily_quotas
Revises: y3_agent_runs_attempt_epoch
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from alembic import op

revision: str = "y4_agent_user_daily_quotas"
down_revision: str | None = "y3_agent_runs_attempt_epoch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 新しいDB権限契約と既存tableへのcolumn追加を含むため、手動適用対象とする。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.create_table(
        "agent_user_daily_quotas",
        sa.Column("user_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth.user.id"],
            ondelete="CASCADE",
            name="fk_agent_user_daily_quotas_user_id",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "usage_date", name="pk_agent_user_daily_quotas"
        ),
        sa.CheckConstraint(
            "used_count >= 0 AND used_count <= 10",
            name="ck_agent_user_daily_quotas_used_count_range",
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column("quota_usage_date", sa.Date(), nullable=True),
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLE agent_user_daily_quotas TO vector_app"
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE "
        "ON TABLE agent_user_daily_quotas FROM vector_app"
    )
    op.drop_column("agent_runs", "quota_usage_date")
    op.drop_table("agent_user_daily_quotas")
