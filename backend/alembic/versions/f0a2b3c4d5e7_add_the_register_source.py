"""Phase 1b: The Register RSS ソース投入。

`specs/source-strategy/roadmap.md` の Phase 1 着手分割 PR-2。
go.theregister.com/feed/ のリダイレクタは fetcher 側で URL 正規化する。

Revision ID: f0a2b3c4d5e7
Revises: e9f1a2b3c4d6
Create Date: 2026-04-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0a2b3c4d5e7"
down_revision: Union[str, None] = "e9f1a2b3c4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_SOURCE_NAME = "The Register"


def upgrade() -> None:
    sources_table = sa.table(
        "news_sources",
        sa.column("name", sa.String),
        sa.column("source_type", sa.String),
        sa.column("endpoint_url", sa.String),
        sa.column("site_url", sa.String),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(
        sources_table,
        [
            {
                "name": _NEW_SOURCE_NAME,
                "source_type": "rss",
                "endpoint_url": "https://www.theregister.com/headlines.atom",
                "site_url": "https://www.theregister.com",
                "is_active": True,
            }
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM news_sources WHERE name = :name"),
        {"name": _NEW_SOURCE_NAME},
    )
