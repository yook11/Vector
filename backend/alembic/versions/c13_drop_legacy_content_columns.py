"""Drop legacy content columns from news_articles.

These columns were superseded during Phase 4:
- content → original_content
- content_fetched_at → original_content IS NOT NULL
- content_fetch_attempts → SimpleRetryMiddleware (Redis)

Revision ID: c13a1b2c3d4e
Revises: c12a1b2c3d4e
Create Date: 2026-03-28
"""

import sqlalchemy as sa
from alembic import op

revision = "c13a1b2c3d4e"
down_revision = "c12a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("news_articles", "content")
    op.drop_column("news_articles", "content_fetched_at")
    op.drop_column("news_articles", "content_fetch_attempts")


def downgrade() -> None:
    op.add_column(
        "news_articles",
        sa.Column(
            "content_fetch_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "news_articles",
        sa.Column(
            "content_fetched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "news_articles",
        sa.Column("content", sa.Text(), nullable=True),
    )
