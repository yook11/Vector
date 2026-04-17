"""name 単独ユニーク制約に変更。

(name, source_type) 複合ユニーク → name 単独ユニーク。
ドメインルール: 1 ソース名 = 1 ソース。

Revision ID: d4e5f6a7b8c9
Revises: c21a1b2c3d4e
Create Date: 2026-04-17
"""

from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c21a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_news_sources_name_source_type", "news_sources", type_="unique"
    )
    op.create_unique_constraint("uq_news_sources_name", "news_sources", ["name"])


def downgrade() -> None:
    op.drop_constraint("uq_news_sources_name", "news_sources", type_="unique")
    op.create_unique_constraint(
        "uq_news_sources_name_source_type",
        "news_sources",
        ["name", "source_type"],
    )
