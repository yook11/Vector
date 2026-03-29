"""add news_articles check constraints

Revision ID: 7b90b86f5207
Revises: d20a1b2c3d4e
Create Date: 2026-03-30 08:03:02.043708

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "7b90b86f5207"
down_revision: Union[str, None] = "d20a1b2c3d4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_news_articles_url_scheme",
        "news_articles",
        "original_url ~ '^https?://.+'",
    )
    op.create_check_constraint(
        "ck_news_articles_title_not_empty",
        "news_articles",
        "original_title != ''",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_news_articles_title_not_empty", "news_articles", type_="check"
    )
    op.drop_constraint(
        "ck_news_articles_url_scheme", "news_articles", type_="check"
    )
