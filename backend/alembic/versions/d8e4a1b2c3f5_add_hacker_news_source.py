"""Phase 0: Hacker News を news_sources に登録。

registry.py には Hacker News フェッチャーが登録済みだが、news_sources
テーブルへの INSERT migration が過去に一度も無く、`is_active=true` の
行が存在しないためスケジューラから呼び出されない状態だった。
本 migration で初投入する。

Revision ID: d8e4a1b2c3f5
Revises: 109044f13a69
Create Date: 2026-04-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8e4a1b2c3f5"
down_revision: Union[str, None] = "109044f13a69"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HN_SOURCE = {
    "name": "Hacker News",
    "source_type": "api",
    "endpoint_url": "https://hn.algolia.com/api/v1",
    "site_url": "https://news.ycombinator.com",
    "is_active": True,
}


def upgrade() -> None:
    sources_table = sa.table(
        "news_sources",
        sa.column("name", sa.String),
        sa.column("source_type", sa.String),
        sa.column("endpoint_url", sa.String),
        sa.column("site_url", sa.String),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(sources_table, [_HN_SOURCE])


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM news_sources WHERE name = :name"),
        {"name": _HN_SOURCE["name"]},
    )
