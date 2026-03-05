"""drop news_sources.category_id column

Revision ID: a4b5c6d7e8f0
Revises: a3b4c5d6e7f9
Create Date: 2026-03-03 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4b5c6d7e8f0"
down_revision: Union[str, None] = "a3b4c5d6e7f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "news_sources_category_id_fkey", "news_sources", type_="foreignkey"
    )
    op.drop_column("news_sources", "category_id")


def downgrade() -> None:
    op.add_column(
        "news_sources",
        sa.Column("category_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "news_sources_category_id_fkey",
        "news_sources",
        "keyword_categories",
        ["category_id"],
        ["id"],
        ondelete="CASCADE",
    )
