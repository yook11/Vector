"""news_articles テーブルを削除。

テーブル分離が完了し、discovered_articles + articles に
すべてのデータが移行済みのため、旧テーブルを削除する。

Revision ID: b2c3d4e5f6g7
Revises: d1e2f3a4b5c6
Create Date: 2026-04-20
"""

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6g7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("news_articles")


def downgrade() -> None:
    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("original_title", sa.String(500), nullable=False),
        sa.Column("original_url", sa.String(), nullable=False),
        sa.Column("original_content", sa.Text(), nullable=True),
        sa.Column("original_description", sa.String(2000), nullable=True),
        sa.Column(
            "news_source_id",
            sa.Integer(),
            sa.ForeignKey("news_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "skip_content_fetch",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.UniqueConstraint("original_url", name="uq_news_articles_original_url"),
        sa.CheckConstraint(
            "original_url ~ '^https?://.+'",
            name="ck_news_articles_url_scheme",
        ),
        sa.CheckConstraint(
            "original_title != ''",
            name="ck_news_articles_title_not_empty",
        ),
        sa.Index("idx_news_published", "published_at"),
        sa.Index(
            "idx_content_fetch_pending",
            "skip_content_fetch",
            postgresql_where=sa.text(
                "original_content IS NULL AND skip_content_fetch = false"
            ),
        ),
        sa.Index(
            "idx_news_source_published",
            "news_source_id",
            sa.text("published_at DESC"),
        ),
    )

    # news_articles にデータを復元
    op.execute("""
        INSERT INTO news_articles (id, original_title, original_url, news_source_id, published_at, created_at)
        SELECT da.id, da.original_title, da.original_url, da.news_source_id, NULL, da.discovered_at
        FROM discovered_articles da
    """)
    op.execute("""
        UPDATE news_articles na
        SET original_content = a.original_content,
            published_at = a.published_at
        FROM articles a
        WHERE a.id = na.id
    """)
