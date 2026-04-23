"""add topic_id analyzed_at composite index on article_analyses.

サイドバーの直近 24 時間集計クエリ向けに (topic_id, analyzed_at) の
複合インデックスを追加する。既存の topic_id 単独インデックスは他クエリ
向けに当面残す。

Revision ID: 640eb6c829eb
Revises: d5e6f7a8b9ca
Create Date: 2026-04-23 11:42:35.297770

"""

from alembic import op

revision: str = "640eb6c829eb"
down_revision: str | None = "d5e6f7a8b9ca"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_article_analyses_topic_id_analyzed_at",
        "article_analyses",
        ["topic_id", "analyzed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_article_analyses_topic_id_analyzed_at",
        table_name="article_analyses",
    )
