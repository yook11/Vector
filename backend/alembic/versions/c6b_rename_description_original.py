"""Rename description_original to original_description for naming consistency.

All 'original_*' columns now follow the same prefix convention:
original_title, original_url, original_content, original_description.

Revision ID: c6b1a2b3c4d5
Revises: c6a1b2c3d4e5
Create Date: 2026-03-25 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6b1a2b3c4d5"
down_revision: str | None = "c6a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "news_articles",
        "description_original",
        new_column_name="original_description",
    )


def downgrade() -> None:
    op.alter_column(
        "news_articles",
        "original_description",
        new_column_name="description_original",
    )
