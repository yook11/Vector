"""Add CHECK constraints for categories slug format and name not empty.

- slug must match ^[a-z0-9][a-z0-9_]{0,49}$ (mirrors CategorySlug VO)
- name must have char_length >= 1 (empty string prevention)

Revision ID: c15a1b2c3d4e
Revises: c14a1b2c3d4e
Create Date: 2026-03-28
"""

from alembic import op

revision = "c15a1b2c3d4e"
down_revision = "c14a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_categories_slug_format",
        "categories",
        "slug ~ '^[a-z0-9][a-z0-9_]{0,49}$'",
    )
    op.create_check_constraint(
        "ck_categories_name_not_empty",
        "categories",
        "char_length(name) >= 1",
    )


def downgrade() -> None:
    op.drop_constraint("ck_categories_name_not_empty", "categories", type_="check")
    op.drop_constraint("ck_categories_slug_format", "categories", type_="check")
