"""add keyword categories, translation tables, remove keywords.category/is_active

Revision ID: g2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-02-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g2b3c4d5e6f7'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. keyword_categories master table ---
    op.create_table('keyword_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('slug', sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index('ix_keyword_categories_slug', 'keyword_categories', ['slug'])

    # --- 2. keyword_category_translations ---
    op.create_table('keyword_category_translations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('locale', sa.String(length=10), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['keyword_categories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category_id', 'locale', name='uq_keyword_cat_locale'),
    )

    # --- 3. keyword_category_links (M:N) ---
    op.create_table('keyword_category_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('keyword_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['keyword_id'], ['keywords.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['category_id'], ['keyword_categories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('keyword_id', 'category_id', name='uq_keyword_category'),
    )

    # --- 4. investment_category_translations ---
    op.create_table('investment_category_translations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('locale', sa.String(length=10), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['category_id'], ['investment_categories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category_id', 'locale', name='uq_invest_cat_locale'),
    )

    # --- 5. Migrate investment_categories data to translations ---
    # Read existing data and insert into translations table
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, slug, name_ja, name_en, description FROM investment_categories")
    ).fetchall()

    # English descriptions keyed by slug (description column stores English)
    en_descriptions = {
        'competitive_edge': 'Technology breakthroughs, patent acquisitions, market share expansion',
        'financial_signal': 'Earnings, revenue changes, profit margins, fundraising',
        'growth_catalyst': 'New products, market expansion, partnerships suggesting growth',
        'market_disruption': 'Threats to existing markets from new technology, industry restructuring',
        'regulatory_shift': 'New regulations, policy changes, subsidies, export controls',
        'risk_mitigation': 'Litigation wins, regulatory clearance, safety confirmations',
    }
    ja_descriptions = {
        'competitive_edge': '技術突破、特許取得、市場シェア拡大',
        'financial_signal': '決算、売上変化、利益率、資金調達',
        'growth_catalyst': '新製品、市場拡大、提携など成長を示唆するニュース',
        'market_disruption': '新技術による既存市場への脅威、業界再編',
        'regulatory_shift': '新法規、政策変更、補助金、輸出規制',
        'risk_mitigation': '訴訟勝訴、規制クリア、安全性確認など',
    }

    invest_trans = sa.table(
        'investment_category_translations',
        sa.column('category_id', sa.Integer),
        sa.column('locale', sa.String),
        sa.column('name', sa.String),
        sa.column('description', sa.Text),
    )
    for row in rows:
        cat_id, slug, name_ja, name_en, desc = row
        op.bulk_insert(invest_trans, [
            {
                'category_id': cat_id,
                'locale': 'ja',
                'name': name_ja,
                'description': ja_descriptions.get(slug, desc),
            },
            {
                'category_id': cat_id,
                'locale': 'en',
                'name': name_en,
                'description': en_descriptions.get(slug, desc),
            },
        ])

    # --- 6. Drop old columns from investment_categories ---
    op.drop_column('investment_categories', 'name_ja')
    op.drop_column('investment_categories', 'name_en')
    op.drop_column('investment_categories', 'description')

    # --- 7. Drop old columns from keywords ---
    op.drop_column('keywords', 'category')
    op.drop_column('keywords', 'is_active')

    # --- 8. Seed keyword_categories + translations ---
    kw_cats = sa.table(
        'keyword_categories',
        sa.column('id', sa.Integer),
        sa.column('slug', sa.String),
    )
    kw_cat_trans = sa.table(
        'keyword_category_translations',
        sa.column('category_id', sa.Integer),
        sa.column('locale', sa.String),
        sa.column('name', sa.String),
    )

    seed_data = [
        (1, 'ai_ml', 'AI・ML', 'AI & ML'),
        (2, 'biotech', 'バイオテック', 'Biotech'),
        (3, 'energy', 'エネルギー', 'Energy'),
        (4, 'fintech', 'フィンテック', 'Fintech'),
        (5, 'materials', '素材科学', 'Materials Science'),
        (6, 'quantum', '量子コンピュータ', 'Quantum Computing'),
        (7, 'robotics', 'ロボティクス', 'Robotics'),
        (8, 'semiconductor', '半導体', 'Semiconductor'),
        (9, 'space', '宇宙', 'Space'),
        (10, 'telecom', '通信', 'Telecom'),
    ]

    op.bulk_insert(kw_cats, [
        {'id': cat_id, 'slug': slug}
        for cat_id, slug, _, _ in seed_data
    ])
    trans_rows = []
    for cat_id, _, name_ja, name_en in seed_data:
        trans_rows.append({'category_id': cat_id, 'locale': 'ja', 'name': name_ja})
        trans_rows.append({'category_id': cat_id, 'locale': 'en', 'name': name_en})
    op.bulk_insert(kw_cat_trans, trans_rows)


def downgrade() -> None:
    # Re-add columns to keywords
    op.add_column('keywords', sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')))
    op.add_column('keywords', sa.Column('category', sa.String(length=50), nullable=False, server_default='custom'))

    # Re-add columns to investment_categories
    op.add_column('investment_categories', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('investment_categories', sa.Column('name_en', sa.String(length=100), nullable=False, server_default=''))
    op.add_column('investment_categories', sa.Column('name_ja', sa.String(length=100), nullable=False, server_default=''))

    # Migrate translations back
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("""
            SELECT t.category_id, t.locale, t.name, t.description
            FROM investment_category_translations t
        """)
    ).fetchall()
    for row in rows:
        cat_id, locale, name, desc = row
        if locale == 'ja':
            conn.execute(sa.text(
                "UPDATE investment_categories SET name_ja = :name, description = :desc WHERE id = :id"
            ), {'name': name, 'desc': desc, 'id': cat_id})
        elif locale == 'en':
            conn.execute(sa.text(
                "UPDATE investment_categories SET name_en = :name WHERE id = :id"
            ), {'name': name, 'id': cat_id})

    # Drop new tables
    op.drop_table('keyword_category_links')
    op.drop_table('keyword_category_translations')
    op.drop_index('ix_keyword_categories_slug', table_name='keyword_categories')
    op.drop_table('keyword_categories')
    op.drop_table('investment_category_translations')
