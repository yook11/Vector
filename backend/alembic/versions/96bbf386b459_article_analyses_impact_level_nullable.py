"""article_analyses_impact_level_nullable

Phase 1 of impact_level removal: drop CHECK constraint and make the column
nullable so the AI pipeline can stop writing values without breaking inserts.
The column itself is dropped in Phase 3.

Revision ID: 96bbf386b459
Revises: d3996317ee0b
Create Date: 2026-04-25 09:44:04.811312

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "96bbf386b459"
down_revision: str | None = "d3996317ee0b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_constraint(
        "ck_article_analyses_impact_level",
        "article_analyses",
        type_="check",
    )
    op.alter_column(
        "article_analyses",
        "impact_level",
        existing_type=sa.String(length=20),
        nullable=True,
    )


def downgrade() -> None:
    """Restore NOT NULL + CHECK constraint on impact_level.

    Important: Rows written during the Phase 1 window will have NULL values.
    Before running this downgrade, backfill them with a placeholder, e.g.
        UPDATE article_analyses SET impact_level='low' WHERE impact_level IS NULL;
    Otherwise the NOT NULL alter will fail.
    """
    op.alter_column(
        "article_analyses",
        "impact_level",
        existing_type=sa.String(length=20),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_article_analyses_impact_level",
        "article_analyses",
        "impact_level IN ('low', 'medium', 'high', 'critical')",
    )
