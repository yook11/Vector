"""add fetch_logs check constraints

Revision ID: 42a07fc6d36d
Revises: a7e2c1f4b830
Create Date: 2026-03-30 14:11:06.566299

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "42a07fc6d36d"
down_revision: Union[str, None] = "a7e2c1f4b830"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_fetch_logs_status",
        "fetch_logs",
        "status IN ('success', 'error')",
    )
    op.create_check_constraint(
        "ck_fetch_logs_articles_count_non_negative",
        "fetch_logs",
        "articles_count >= 0",
    )
    op.create_check_constraint(
        "ck_fetch_logs_duration_ms_non_negative",
        "fetch_logs",
        "duration_ms IS NULL OR duration_ms >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_fetch_logs_duration_ms_non_negative", "fetch_logs")
    op.drop_constraint("ck_fetch_logs_articles_count_non_negative", "fetch_logs")
    op.drop_constraint("ck_fetch_logs_status", "fetch_logs")
