"""Phase 7c-2: drop guid column from news_articles.

guid-based dedup has been replaced by URL-based dedup (original_url).
The guid column is no longer written or read by any service.

Revision ID: c11a1b2c3d4e
Revises: c10a1b2c3d4e
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "c11a1b2c3d4e"
down_revision = "c10a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_news_articles_guid", "news_articles", type_="unique")
    op.drop_column("news_articles", "guid")


def downgrade() -> None:
    op.add_column(
        "news_articles",
        sa.Column("guid", sa.String(length=2048), nullable=True),
    )
    op.create_unique_constraint("uq_news_articles_guid", "news_articles", ["guid"])
