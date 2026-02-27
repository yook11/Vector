"""add analysis_translations table, remove title_ja/summary_ja/key_topics from analyses

Revision ID: h3c4d5e6f7g8
Revises: g2b3c4d5e6f7
Create Date: 2026-02-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'h3c4d5e6f7g8'
down_revision: Union[str, None] = 'g2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create analysis_translations table
    op.create_table(
        'analysis_translations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('analysis_id', sa.Integer(), nullable=False),
        sa.Column('locale', sa.String(length=10), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ['analysis_id'], ['analyses.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('analysis_id', 'locale', name='uq_analysis_locale'),
    )

    # 2. Migrate existing data: copy title_ja/summary_ja into translations
    op.execute(
        "INSERT INTO analysis_translations (analysis_id, locale, title, summary) "
        "SELECT id, 'ja', title_ja, summary_ja FROM analyses"
    )

    # 3. Drop old columns from analyses
    op.drop_column('analyses', 'title_ja')
    op.drop_column('analyses', 'summary_ja')
    op.drop_column('analyses', 'key_topics')


def downgrade() -> None:
    # 1. Re-add columns to analyses
    op.add_column(
        'analyses',
        sa.Column('title_ja', sa.String(length=500), nullable=True),
    )
    op.add_column(
        'analyses',
        sa.Column('summary_ja', sa.Text(), nullable=True),
    )
    op.add_column(
        'analyses',
        sa.Column(
            'key_topics',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # 2. Restore data from translations
    op.execute(
        "UPDATE analyses SET title_ja = t.title, summary_ja = t.summary "
        "FROM analysis_translations t "
        "WHERE analyses.id = t.analysis_id AND t.locale = 'ja'"
    )

    # 3. Set NOT NULL constraints back
    op.alter_column('analyses', 'title_ja', nullable=False)
    op.alter_column('analyses', 'summary_ja', nullable=False)

    # 4. Drop translations table
    op.drop_table('analysis_translations')
