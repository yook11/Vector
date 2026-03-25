"""Drop watchlists and create watchlist_entries with composite PK and auth.user FK.

Revision ID: c7a1b2c3d4e5
Revises: c6b1a2b3c4d5
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "c7a1b2c3d4e5"
down_revision = "c6b1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("watchlists")
    op.create_table(
        "watchlist_entries",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("news_article_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "news_article_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth.user.id"],
            name="fk_watchlist_entries_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_article_id"],
            ["news_articles.id"],
            name="fk_watchlist_entries_news_article_id",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("watchlist_entries")
    op.create_table(
        "watchlists",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(32), nullable=False, index=True),
        sa.Column("news_article_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "news_article_id", name="uq_user_watchlist"),
        sa.ForeignKeyConstraint(
            ["news_article_id"],
            ["news_articles.id"],
            ondelete="CASCADE",
        ),
    )
