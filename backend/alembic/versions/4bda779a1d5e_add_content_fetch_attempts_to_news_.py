"""add content_fetch_attempts to news_articles

Revision ID: 4bda779a1d5e
Revises: h3c4d5e6f7g8
Create Date: 2026-02-28 00:33:44.070407

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4bda779a1d5e'
down_revision: Union[str, None] = 'h3c4d5e6f7g8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'news_articles',
        sa.Column('content_fetch_attempts', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('news_articles', 'content_fetch_attempts')
