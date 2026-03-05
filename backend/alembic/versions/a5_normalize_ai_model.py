"""normalize ai_model: create ai_models table, migrate analyses FK

Revision ID: a5b6c7d8e9f0
Revises: a4b5c6d7e8f0
Create Date: 2026-03-05 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, None] = "a4b5c6d7e8f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create ai_models table
    op.create_table(
        "ai_models",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.UniqueConstraint("provider", "name", name="uq_ai_model_provider_name"),
    )

    # 2. Seed from existing analyses data
    op.execute(
        """
        INSERT INTO ai_models (provider, name)
        SELECT DISTINCT ai_provider, ai_model
        FROM analyses
        WHERE ai_provider IS NOT NULL AND ai_model IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )

    # 3. Add ai_model_id column (nullable initially)
    op.add_column(
        "analyses",
        sa.Column("ai_model_id", sa.Integer(), nullable=True),
    )

    # 4. Backfill ai_model_id from existing columns
    op.execute(
        """
        UPDATE analyses
        SET ai_model_id = ai_models.id
        FROM ai_models
        WHERE analyses.ai_provider = ai_models.provider
          AND analyses.ai_model = ai_models.name
        """
    )

    # 5. Make ai_model_id NOT NULL + add FK
    op.alter_column("analyses", "ai_model_id", nullable=False)
    op.create_foreign_key(
        "fk_analyses_ai_model_id",
        "analyses",
        "ai_models",
        ["ai_model_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 6. Drop old UNIQUE constraint on news_article_id
    op.drop_constraint("analyses_news_article_id_key", "analyses", type_="unique")

    # 7. Add composite UNIQUE (news_article_id, ai_model_id)
    op.create_unique_constraint(
        "uq_analyses_article_model",
        "analyses",
        ["news_article_id", "ai_model_id"],
    )

    # 8. Add index on ai_model_id
    op.create_index("idx_analyses_ai_model_id", "analyses", ["ai_model_id"])

    # 9. Drop old columns
    op.drop_column("analyses", "ai_provider")
    op.drop_column("analyses", "ai_model")


def downgrade() -> None:
    # 1. Re-add old columns
    op.add_column(
        "analyses",
        sa.Column("ai_provider", sa.String(20), nullable=True),
    )
    op.add_column(
        "analyses",
        sa.Column("ai_model", sa.String(50), nullable=True),
    )

    # 2. Backfill from ai_models
    op.execute(
        """
        UPDATE analyses
        SET ai_provider = ai_models.provider,
            ai_model = ai_models.name
        FROM ai_models
        WHERE analyses.ai_model_id = ai_models.id
        """
    )

    # 3. Make old columns NOT NULL
    op.alter_column("analyses", "ai_provider", nullable=False)
    op.alter_column("analyses", "ai_model", nullable=False)

    # 4. Drop new index
    op.drop_index("idx_analyses_ai_model_id", table_name="analyses")

    # 5. Drop composite UNIQUE
    op.drop_constraint("uq_analyses_article_model", "analyses", type_="unique")

    # 6. Restore original UNIQUE on news_article_id
    op.create_unique_constraint(
        "analyses_news_article_id_key",
        "analyses",
        ["news_article_id"],
    )

    # 7. Drop FK and column
    op.drop_constraint("fk_analyses_ai_model_id", "analyses", type_="foreignkey")
    op.drop_column("analyses", "ai_model_id")

    # 8. Drop ai_models table
    op.drop_table("ai_models")
