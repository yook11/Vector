"""remove entity type enum constraint and widen column

Revision ID: 1250a92960a5
Revises: c3d4e5f6g7h8
Create Date: 2026-04-20 09:52:25.287936

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1250a92960a5'
down_revision: Union[str, None] = 'c3d4e5f6g7h8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_article_entities_type_valid", "article_entities", type_="check"
    )
    op.alter_column(
        "article_entities",
        "type",
        existing_type=sa.VARCHAR(length=20),
        type_=sa.String(length=50),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "article_entities",
        "type",
        existing_type=sa.String(length=50),
        type_=sa.VARCHAR(length=20),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_article_entities_type_valid",
        "article_entities",
        "type IN ('company', 'product', 'technology')",
    )
