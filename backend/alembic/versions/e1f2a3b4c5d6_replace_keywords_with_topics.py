"""Keyword を Topic に置き換え。

既存データを全削除し、keywords/article_keywords テーブルを削除。
topics テーブルを新規作成し、article_analyses に topic_id を追加。

注意: TRUNCATE CASCADE により watchlist_entries も削除される。

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 既存データクリア (FK 依存順)
    # watchlist_entries は article_analyses への FK CASCADE で自動削除される
    op.execute("TRUNCATE article_keywords CASCADE")
    op.execute("TRUNCATE article_analyses CASCADE")
    op.execute("TRUNCATE news_articles CASCADE")

    # 2. 旧テーブル削除
    op.drop_table("article_keywords")
    op.drop_table("keywords")

    # 3. topics テーブル作成
    op.create_table(
        "topics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("categories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("name", "category_id", name="uq_topics_name_category_id"),
    )
    op.create_index("ix_topics_category_id", "topics", ["category_id"])

    # 4. article_analyses に topic_id 追加 (NOT NULL)
    op.add_column(
        "article_analyses",
        sa.Column(
            "topic_id",
            sa.Integer(),
            sa.ForeignKey("topics.id", ondelete="RESTRICT"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_article_analyses_topic_id", "article_analyses", ["topic_id"]
    )


def downgrade() -> None:
    # article_analyses から topic_id を削除
    op.drop_index("ix_article_analyses_topic_id", table_name="article_analyses")
    op.drop_column("article_analyses", "topic_id")

    # topics テーブル削除
    op.drop_index("ix_topics_category_id", table_name="topics")
    op.drop_table("topics")

    # keywords + article_keywords を再作成
    op.create_table(
        "keywords",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("categories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="provisional"),
        sa.Column("is_ai_generated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_keywords_category_id", "keywords", ["category_id"])

    op.create_table(
        "article_keywords",
        sa.Column(
            "article_analysis_id",
            sa.Integer(),
            sa.ForeignKey("article_analyses.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "keyword_id",
            sa.Integer(),
            sa.ForeignKey("keywords.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
