"""add investment categories

Revision ID: f1a2b3c4d5e6
Revises: a1b2c3d4e5f6
Create Date: 2026-02-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('investment_categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=50), nullable=False),
    sa.Column('name_ja', sa.String(length=100), nullable=False),
    sa.Column('name_en', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug'),
    )
    op.create_index('ix_investment_categories_slug', 'investment_categories', ['slug'])

    op.create_table('analysis_investment_categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('analysis_id', sa.Integer(), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['analysis_id'], ['analyses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['category_id'], ['investment_categories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('analysis_id', 'category_id', name='uq_analysis_category'),
    )

    # Seed initial categories
    investment_categories = sa.table(
        'investment_categories',
        sa.column('slug', sa.String),
        sa.column('name_ja', sa.String),
        sa.column('name_en', sa.String),
        sa.column('description', sa.Text),
    )
    op.bulk_insert(investment_categories, [
        {
            'slug': 'competitive_edge',
            'name_ja': '競争優位',
            'name_en': 'Competitive Edge',
            'description': 'Technology breakthroughs, patent acquisitions, market share expansion',
        },
        {
            'slug': 'financial_signal',
            'name_ja': '業績シグナル',
            'name_en': 'Financial Signal',
            'description': 'Earnings, revenue changes, profit margins, fundraising',
        },
        {
            'slug': 'growth_catalyst',
            'name_ja': '成長期待',
            'name_en': 'Growth Catalyst',
            'description': 'New products, market expansion, partnerships suggesting growth',
        },
        {
            'slug': 'market_disruption',
            'name_ja': '市場破壊',
            'name_en': 'Market Disruption',
            'description': 'Threats to existing markets from new technology, industry restructuring',
        },
        {
            'slug': 'regulatory_shift',
            'name_ja': '規制変化',
            'name_en': 'Regulatory Shift',
            'description': 'New regulations, policy changes, subsidies, export controls',
        },
        {
            'slug': 'risk_mitigation',
            'name_ja': 'リスク回避',
            'name_en': 'Risk Mitigation',
            'description': 'Litigation wins, regulatory clearance, safety confirmations',
        },
    ])


def downgrade() -> None:
    op.drop_table('analysis_investment_categories')
    op.drop_index('ix_investment_categories_slug', table_name='investment_categories')
    op.drop_table('investment_categories')
