"""Add skip_content_fetch column and partial index for content fetch pending.

Supports the new pipeline chain architecture (Step 2): articles that permanently
fail content fetch are flagged with skip_content_fetch=True so that
dispatch_pending never re-enqueues them.

Revision ID: c12a1b2c3d4e
Revises: c11a1b2c3d4e
Create Date: 2026-03-28
"""

import sqlalchemy as sa
from alembic import op

revision = "c12a1b2c3d4e"
down_revision = "c11a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "news_articles",
        sa.Column(
            "skip_content_fetch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "idx_content_fetch_pending",
        "news_articles",
        ["skip_content_fetch"],
        postgresql_where=sa.text(
            "original_content IS NULL AND skip_content_fetch = FALSE"
        ),
    )


def downgrade() -> None:
    op.drop_index("idx_content_fetch_pending", table_name="news_articles")
    op.drop_column("news_articles", "skip_content_fetch")
