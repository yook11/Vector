"""Phase 7b: Drop ai_models table.

Revision ID: c9a1b2c3d4e5
Revises: c8a1b2c3d4e5
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "c9a1b2c3d4e5"
down_revision = "c8a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop FK from analyses before dropping ai_models
    op.drop_constraint("fk_analyses_ai_model_id", "analyses", type_="foreignkey")
    op.drop_column("analyses", "ai_model_id")
    op.drop_table("ai_models")


def downgrade() -> None:
    op.create_table(
        "ai_models",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.UniqueConstraint("provider", "name", name="uq_ai_model_provider_name"),
    )
    op.add_column(
        "analyses",
        sa.Column("ai_model_id", sa.Integer(), nullable=False),
    )
    op.create_foreign_key(
        "fk_analyses_ai_model_id",
        "analyses",
        "ai_models",
        ["ai_model_id"],
        ["id"],
    )
