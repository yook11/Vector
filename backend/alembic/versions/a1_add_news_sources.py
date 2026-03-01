"""add news_sources table

Revision ID: a1b2c3d4e5f7
Revises: f52d4ecebe6b
Create Date: 2026-03-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, None] = "f52d4ecebe6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("site_url", sa.String(length=2048), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "fetch_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="720",
        ),
        sa.Column(
            "next_fetch_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "last_fetched_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "consecutive_errors", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        # RSS-specific
        sa.Column("feed_url", sa.String(length=2048), nullable=True),
        sa.Column("etag", sa.String(length=256), nullable=True),
        sa.Column("last_modified_header", sa.String(length=256), nullable=True),
        # API-specific
        sa.Column("api_endpoint", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["keyword_categories.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("feed_url"),
        # CHECK: source_type must be 'rss' or 'api'
        sa.CheckConstraint(
            "source_type IN ('rss', 'api')",
            name="ck_news_sources_source_type",
        ),
        # CHECK: RSS requires feed_url, API requires api_endpoint
        sa.CheckConstraint(
            "(source_type = 'rss' AND feed_url IS NOT NULL) "
            "OR (source_type = 'api' AND api_endpoint IS NOT NULL)",
            name="ck_news_sources_type_fields",
        ),
        # CHECK: fetch_interval_minutes between 15 and 1440
        sa.CheckConstraint(
            "fetch_interval_minutes BETWEEN 15 AND 1440",
            name="ck_news_sources_interval_range",
        ),
    )

    # Partial index for scheduler: find active sources due for fetching
    op.create_index(
        "idx_sources_active_next_fetch",
        "news_sources",
        ["next_fetch_at"],
        postgresql_where=sa.text("is_active = TRUE"),
    )


def downgrade() -> None:
    op.drop_index("idx_sources_active_next_fetch", table_name="news_sources")
    op.drop_table("news_sources")
