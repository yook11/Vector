"""Fix naming conventions and add missing index.

1. categories: drop legacy unique constraint keyword_categories_slug_key,
   upgrade ix_categories_slug to unique (replaces the old constraint)
2. fetch_logs: add ix_fetch_logs_source_id (c18 dropped the composite index,
   so the single-column index defined in the model was missing)

Revision ID: c19a1b2c3d4e
Revises: c18a1b2c3d4e
Create Date: 2026-03-29 13:30:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c19a1b2c3d4e"
down_revision: Union[str, None] = "c18a1b2c3d4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. categories: replace legacy unique constraint with unique index
    op.drop_constraint(
        "keyword_categories_slug_key", "categories", type_="unique"
    )
    op.drop_index("ix_categories_slug", table_name="categories")
    op.create_index(
        "ix_categories_slug", "categories", ["slug"], unique=True
    )

    # 2. fetch_logs: add single-column source_id index
    op.create_index(
        "ix_fetch_logs_source_id", "fetch_logs", ["source_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_fetch_logs_source_id", table_name="fetch_logs")
    op.drop_index("ix_categories_slug", table_name="categories")
    op.create_index(
        "ix_categories_slug", "categories", ["slug"], unique=False
    )
    op.create_unique_constraint(
        "keyword_categories_slug_key", "categories", ["slug"]
    )
