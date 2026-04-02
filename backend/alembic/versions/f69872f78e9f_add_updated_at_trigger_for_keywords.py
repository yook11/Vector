"""add_updated_at_trigger_for_keywords

Revision ID: f69872f78e9f
Revises: 42a07fc6d36d
Create Date: 2026-04-02 11:43:50.142816

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f69872f78e9f'
down_revision: Union[str, None] = '42a07fc6d36d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Reuse the shared trigger function created in d20a1b2c3d4e
    op.execute("""
        CREATE TRIGGER trg_keywords_updated_at
        BEFORE UPDATE ON keywords
        FOR EACH ROW
        EXECUTE FUNCTION update_updated_at_column();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_keywords_updated_at ON keywords;")
