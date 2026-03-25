"""Keywords and article_keywords redesign.

Phase 2 of DB redesign:
- keywords.keyword -> keywords.name (VARCHAR(200) -> VARCHAR(100))
- keywords.category_id FK added (M:N -> 1:N, data migrated from keyword_category_links)
- keywords.status, is_ai_generated, approved_at added
- keyword_category_links dropped
- news_keywords -> article_keywords (surrogate PK -> composite PK)
- user_keyword_subscriptions dropped

Revision ID: c2a1b2c3d4e5
Revises: c1a2b3c4d5e6
Create Date: 2026-03-24 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c2a1b2c3d4e5"
down_revision = "c1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # === keywords table changes ===

    # 1. Rename keyword -> name
    op.alter_column("keywords", "keyword", new_column_name="name")

    # 2. Shrink VARCHAR(200) -> VARCHAR(100)
    op.alter_column(
        "keywords",
        "name",
        type_=sa.String(100),
        existing_type=sa.String(200),
        existing_nullable=False,
    )

    # 3. Add CHECK constraint on name
    op.create_check_constraint(
        "ck_keywords_name_length",
        "keywords",
        "char_length(trim(name)) >= 1 AND char_length(name) <= 100",
    )

    # 4. Rename unique constraint on name
    op.execute(
        "ALTER INDEX IF EXISTS ix_keywords_keyword RENAME TO ix_keywords_name"
    )

    # 5. Add category_id (nullable initially for data migration)
    op.add_column(
        "keywords",
        sa.Column("category_id", sa.Integer, nullable=True),
    )

    # 6. Data migration: copy category_id from keyword_category_links
    op.execute(
        """
        UPDATE keywords k
        SET category_id = (
            SELECT MIN(kcl.category_id)
            FROM keyword_category_links kcl
            WHERE kcl.keyword_id = k.id
        )
        """
    )

    # 7. Handle keywords without category links (assign to first category)
    op.execute(
        """
        UPDATE keywords
        SET category_id = (SELECT MIN(id) FROM categories)
        WHERE category_id IS NULL
        """
    )

    # 8. Make category_id NOT NULL and add FK
    op.alter_column("keywords", "category_id", nullable=False)
    op.create_foreign_key(
        "fk_keywords_category_id",
        "keywords",
        "categories",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_keywords_category_id", "keywords", ["category_id"])

    # 9. Add status column
    op.add_column(
        "keywords",
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="official",
        ),
    )
    op.create_check_constraint(
        "ck_keywords_status",
        "keywords",
        "status IN ('provisional', 'official', 'blacklisted')",
    )

    # 10. Add is_ai_generated column
    op.add_column(
        "keywords",
        sa.Column(
            "is_ai_generated",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # 11. Add approved_at column
    op.add_column(
        "keywords",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 12. Drop keyword_category_links table
    op.drop_table("keyword_category_links")

    # === news_keywords -> article_keywords ===

    # 13. Drop the surrogate PK and unique constraint, then recreate as composite PK
    # First drop the unique constraint
    op.drop_constraint("uq_news_keyword", "news_keywords", type_="unique")

    # Drop the id column (which is the current PK)
    op.drop_column("news_keywords", "id")

    # Add composite primary key
    op.create_primary_key(
        "pk_article_keywords",
        "news_keywords",
        ["news_article_id", "keyword_id"],
    )

    # 14. Rename table
    op.rename_table("news_keywords", "article_keywords")

    # === Drop user_keyword_subscriptions ===

    # 15. Drop user_keyword_subscriptions table
    op.drop_table("user_keyword_subscriptions")


def downgrade() -> None:
    # === Recreate user_keyword_subscriptions ===

    op.create_table(
        "user_keyword_subscriptions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(32), nullable=False, index=True),
        sa.Column(
            "keyword_id",
            sa.Integer,
            sa.ForeignKey("keywords.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "keyword_id", name="uq_user_keyword"),
    )

    # === article_keywords -> news_keywords ===

    # 1. Rename table back
    op.rename_table("article_keywords", "news_keywords")

    # 2. Drop composite PK
    op.drop_constraint("pk_article_keywords", "news_keywords", type_="primary")

    # 3. Add id column back (nullable first, populate, then set NOT NULL + PK)
    op.add_column(
        "news_keywords",
        sa.Column("id", sa.Integer, nullable=True),
    )
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS news_keywords_id_seq"
    )
    op.execute(
        "UPDATE news_keywords "
        "SET id = nextval('news_keywords_id_seq')"
    )
    op.alter_column("news_keywords", "id", nullable=False)
    op.execute(
        "ALTER TABLE news_keywords "
        "ALTER COLUMN id SET DEFAULT nextval('news_keywords_id_seq')"
    )
    op.execute(
        "ALTER SEQUENCE news_keywords_id_seq OWNED BY news_keywords.id"
    )
    op.create_primary_key("news_keywords_pkey", "news_keywords", ["id"])

    # 4. Recreate unique constraint
    op.create_unique_constraint(
        "uq_news_keyword",
        "news_keywords",
        ["news_article_id", "keyword_id"],
    )

    # === Restore keywords table ===

    # 1. Recreate keyword_category_links
    op.create_table(
        "keyword_category_links",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "keyword_id",
            sa.Integer,
            sa.ForeignKey("keywords.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "keyword_id", "category_id", name="uq_keyword_category"
        ),
    )

    # 2. Migrate category_id back to keyword_category_links
    op.execute(
        """
        INSERT INTO keyword_category_links (keyword_id, category_id)
        SELECT id, category_id FROM keywords
        WHERE category_id IS NOT NULL
        """
    )

    # 3. Drop new columns from keywords
    op.drop_column("keywords", "approved_at")
    op.drop_column("keywords", "is_ai_generated")
    op.drop_constraint("ck_keywords_status", "keywords", type_="check")
    op.drop_column("keywords", "status")
    op.drop_index("ix_keywords_category_id", "keywords")
    op.drop_constraint("fk_keywords_category_id", "keywords", type_="foreignkey")
    op.drop_column("keywords", "category_id")

    # 4. Rename name -> keyword and expand VARCHAR
    op.drop_constraint("ck_keywords_name_length", "keywords", type_="check")
    op.execute(
        "ALTER INDEX IF EXISTS ix_keywords_name RENAME TO ix_keywords_keyword"
    )
    op.alter_column(
        "keywords",
        "name",
        type_=sa.String(200),
        existing_type=sa.String(100),
        existing_nullable=False,
    )
    op.alter_column("keywords", "name", new_column_name="keyword")
