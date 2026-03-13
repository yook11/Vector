"""seed default ai_model (gemini-2.5-flash-lite)

Revision ID: a7b8c9d0e1f2
Revises: a6b7c8d9e0f1
Create Date: 2026-03-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "a6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROVIDER = "gemini"
MODEL_NAME = "gemini-2.5-flash-lite"


def upgrade() -> None:
    ai_models = sa.table(
        "ai_models",
        sa.column("provider", sa.String),
        sa.column("name", sa.String),
        sa.column("is_active", sa.Boolean),
    )
    op.execute(
        ai_models.insert().values(
            provider=PROVIDER,
            name=MODEL_NAME,
            is_active=True,
        )
    )
    # NOTE: Check the assigned ID with:
    #   SELECT id FROM ai_models WHERE provider='gemini' AND name='gemini-2.5-flash-lite';
    # Then set DEFAULT_AI_MODEL_ID in .env accordingly.


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM ai_models "
            "WHERE provider = :provider AND name = :name "
            "AND NOT EXISTS (SELECT 1 FROM analyses WHERE ai_model_id = ai_models.id)"
        ).bindparams(provider=PROVIDER, name=MODEL_NAME)
    )
