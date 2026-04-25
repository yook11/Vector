"""drop_article_analyses_impact_level

Phase 3 of impact_level removal: physically drop the column from
article_analyses. Phase 1 stopped writing the column; Phase 2 stopped
reading it; this migration completes the removal.

Irreversible by design: the downgrade re-adds a NULL column, but the
historical values cannot be recovered. Restore from pg_dump if needed.

Revision ID: 9304ea71c183
Revises: 96bbf386b459
Create Date: 2026-04-25 01:46:04.711646

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9304ea71c183"
down_revision: str | None = "96bbf386b459"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_column("article_analyses", "impact_level")


def downgrade() -> None:
    """Re-add impact_level as a nullable column.

    Data is not recovered. Any code revert must precede this downgrade so
    that rolled-back code does not attempt to read the missing values.
    Production recovery should restore from a pg_dump table snapshot
    rather than relying on this downgrade.
    """
    op.add_column(
        "article_analyses",
        sa.Column("impact_level", sa.String(length=20), nullable=True),
    )
