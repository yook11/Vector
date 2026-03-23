"""Rename keyword_categories to categories, drop translation and investment tables.

Phase 1 of DB redesign:
- keyword_categories -> categories (add name column, drop translations)
- Drop investment_categories, investment_category_translations, analysis_investment_categories
- keyword_category_links retained until Phase 2

Revision ID: c1a2b3c4d5e6
Revises: b1a2c3d4e5f6
Create Date: 2026-03-22 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c1a2b3c4d5e6"
down_revision = "b1a2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add name column to keyword_categories (nullable initially for data migration)
    op.add_column(
        "keyword_categories",
        sa.Column("name", sa.String(50), nullable=True),
    )

    # 2. Data migration: copy Japanese names from translations
    op.execute(
        """
        UPDATE keyword_categories kc
        SET name = (
            SELECT kct.name
            FROM keyword_category_translations kct
            WHERE kct.category_id = kc.id AND kct.locale = 'ja'
            LIMIT 1
        )
        """
    )

    # 3. Fallback: fill any remaining NULLs with English names
    op.execute(
        """
        UPDATE keyword_categories kc
        SET name = (
            SELECT kct.name
            FROM keyword_category_translations kct
            WHERE kct.category_id = kc.id
            LIMIT 1
        )
        WHERE kc.name IS NULL
        """
    )

    # 4. Make name NOT NULL and add constraints
    op.alter_column("keyword_categories", "name", nullable=False)
    op.create_unique_constraint("uq_categories_name", "keyword_categories", ["name"])
    op.create_check_constraint(
        "ck_categories_name_length",
        "keyword_categories",
        "char_length(trim(name)) >= 1 AND char_length(name) <= 50",
    )

    # 5. Add CHECK constraint on slug
    op.create_check_constraint(
        "ck_categories_slug_length",
        "keyword_categories",
        "char_length(trim(slug)) >= 1 AND char_length(slug) <= 50",
    )

    # 6. Drop translation table (FK references keyword_categories, safe to drop)
    op.drop_table("keyword_category_translations")

    # 7. Drop investment category related tables (order: children first)
    op.drop_table("analysis_investment_categories")
    op.drop_table("investment_category_translations")
    op.drop_table("investment_categories")

    # 8. Rename keyword_categories -> categories
    op.rename_table("keyword_categories", "categories")

    # 9. Rename constraints/indexes to match new table name
    op.execute(
        "ALTER INDEX IF EXISTS ix_keyword_categories_slug RENAME TO ix_categories_slug"
    )


def downgrade() -> None:
    # 1. Rename categories back to keyword_categories
    op.rename_table("categories", "keyword_categories")

    # 2. Rename index back
    op.execute(
        "ALTER INDEX IF EXISTS ix_categories_slug RENAME TO ix_keyword_categories_slug"
    )

    # 3. Recreate investment_categories
    op.create_table(
        "investment_categories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(50), nullable=False, unique=True, index=True),
    )

    # 4. Recreate investment_category_translations
    op.create_table(
        "investment_category_translations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("investment_categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("locale", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.UniqueConstraint("category_id", "locale", name="uq_invest_cat_locale"),
    )

    # 5. Recreate analysis_investment_categories
    op.create_table(
        "analysis_investment_categories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "analysis_id",
            sa.Integer,
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("investment_categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("analysis_id", "category_id", name="uq_analysis_category"),
    )

    # 6. Recreate keyword_category_translations
    op.create_table(
        "keyword_category_translations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("keyword_categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("locale", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.UniqueConstraint("category_id", "locale", name="uq_keyword_cat_locale"),
    )

    # 7. Migrate name back to translations (ja locale)
    op.execute(
        """
        INSERT INTO keyword_category_translations (category_id, locale, name)
        SELECT id, 'ja', name FROM keyword_categories
        """
    )

    # 8. Drop constraints and name column
    op.drop_constraint("ck_categories_slug_length", "keyword_categories", type_="check")
    op.drop_constraint("ck_categories_name_length", "keyword_categories", type_="check")
    op.drop_constraint("uq_categories_name", "keyword_categories", type_="unique")
    op.drop_column("keyword_categories", "name")
