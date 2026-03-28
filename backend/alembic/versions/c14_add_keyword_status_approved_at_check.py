"""Add CHECK constraint: status-approved_at invariant on keywords.

Business rule:
- OFFICIAL → approved_at IS NOT NULL
- PROVISIONAL / BLACKLISTED → approved_at IS NULL

Revision ID: c14a1b2c3d4e
Revises: c13a1b2c3d4e
Create Date: 2026-03-28
"""

from alembic import op

revision = "c14a1b2c3d4e"
down_revision = "c13a1b2c3d4e"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "ck_keywords_status_approved_at"


def upgrade() -> None:
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "keywords",
        """
        (status = 'official' AND approved_at IS NOT NULL)
        OR
        (status != 'official' AND approved_at IS NULL)
        """,
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "keywords", type_="check")
