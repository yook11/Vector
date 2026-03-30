"""add news_sources name check constraint

Revision ID: a7e2c1f4b830
Revises: 6b3f8ec9a583
Create Date: 2026-03-30 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7e2c1f4b830"
down_revision: str | None = "6b3f8ec9a583"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_news_sources_name_not_empty",
        "news_sources",
        "name != ''",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_news_sources_name_not_empty",
        "news_sources",
    )
