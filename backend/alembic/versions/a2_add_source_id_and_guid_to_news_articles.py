"""add source_id and guid to news_articles

Revision ID: a2b3c4d5e6f8
Revises: a1b2c3d4e5f7
Create Date: 2026-03-01 00:01:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f8"
down_revision: Union[str, None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # source_id: nullable FK to news_sources, ON DELETE SET NULL
    op.add_column(
        "news_articles",
        sa.Column("source_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_news_articles_source_id",
        "news_articles",
        "news_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # guid: nullable unique column for RSS deduplication
    op.add_column(
        "news_articles",
        sa.Column("guid", sa.String(length=2048), nullable=True),
    )
    op.create_unique_constraint("uq_news_articles_guid", "news_articles", ["guid"])

    # Partial index for source-based article lookup
    op.create_index(
        "idx_articles_source_published",
        "news_articles",
        ["source_id", sa.text("published_at DESC")],
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_articles_source_published", table_name="news_articles")
    op.drop_constraint("uq_news_articles_guid", "news_articles", type_="unique")
    op.drop_column("news_articles", "guid")
    op.drop_constraint(
        "fk_news_articles_source_id", "news_articles", type_="foreignkey"
    )
    op.drop_column("news_articles", "source_id")
