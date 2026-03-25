"""Phase 4 Step 3: Tighten constraints on news_articles new columns.

- ALTER NOT NULL: original_title, original_url, news_source_id, created_at
- ADD UNIQUE: original_url
- ADD FK RESTRICT: news_source_id -> news_sources(id)
- ALTER DEFAULT: created_at DEFAULT NOW()
- ALTER TYPE: description_original TEXT -> VARCHAR(2000)
- CREATE INDEX: idx_news_created, idx_news_source_published

Revision ID: c6a1b2c3d4e5
Revises: c5a1b2c3d4e5
Create Date: 2026-03-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6a1b2c3d4e5"
down_revision: str | None = "c5a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- NOT NULL constraints ---
    op.alter_column(
        "news_articles", "original_title", existing_type=sa.String(500), nullable=False
    )
    op.alter_column(
        "news_articles",
        "original_url",
        existing_type=sa.String(2048),
        nullable=False,
    )
    op.alter_column(
        "news_articles", "news_source_id", existing_type=sa.Integer(), nullable=False
    )
    op.alter_column(
        "news_articles",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )

    # --- UNIQUE constraint on original_url ---
    op.create_unique_constraint(
        "uq_news_articles_original_url", "news_articles", ["original_url"]
    )

    # --- FK RESTRICT: news_source_id -> news_sources(id) ---
    op.create_foreign_key(
        "fk_news_articles_news_source_id",
        "news_articles",
        "news_sources",
        ["news_source_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- ALTER TYPE: description_original TEXT -> VARCHAR(2000) ---
    op.alter_column(
        "news_articles",
        "description_original",
        existing_type=sa.Text(),
        type_=sa.String(2000),
    )

    # --- Indexes ---
    op.create_index("idx_news_created", "news_articles", ["created_at"])
    op.create_index(
        "idx_news_source_published",
        "news_articles",
        ["news_source_id", sa.text("published_at DESC")],
    )


def downgrade() -> None:
    # --- Drop indexes ---
    op.drop_index("idx_news_source_published", table_name="news_articles")
    op.drop_index("idx_news_created", table_name="news_articles")

    # --- Revert description_original to TEXT ---
    op.alter_column(
        "news_articles",
        "description_original",
        existing_type=sa.String(2000),
        type_=sa.Text(),
    )

    # --- Drop FK ---
    op.drop_constraint(
        "fk_news_articles_news_source_id", "news_articles", type_="foreignkey"
    )

    # --- Drop UNIQUE ---
    op.drop_constraint("uq_news_articles_original_url", "news_articles", type_="unique")

    # --- Revert to NULLABLE ---
    op.alter_column(
        "news_articles",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "news_articles", "news_source_id", existing_type=sa.Integer(), nullable=True
    )
    op.alter_column(
        "news_articles",
        "original_url",
        existing_type=sa.String(2048),
        nullable=True,
    )
    op.alter_column(
        "news_articles", "original_title", existing_type=sa.String(500), nullable=True
    )
