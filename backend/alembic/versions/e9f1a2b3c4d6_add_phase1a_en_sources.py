"""Phase 1a: 英語 RSS 軽量 4 ソース投入 (Engadget / CleanTechnica / Electrek / SpaceNews)。

`specs/source-strategy/roadmap.md` の Phase 1 着手分割 PR-1。
全て BaseRssFetcher 継承のみで実装可能な構造同型ソース。

Revision ID: e9f1a2b3c4d6
Revises: d8e4a1b2c3f5
Create Date: 2026-04-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9f1a2b3c4d6"
down_revision: Union[str, None] = "d8e4a1b2c3f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (name, source_type, endpoint_url, site_url)
_NEW_SOURCES = [
    (
        "Engadget",
        "rss",
        "https://www.engadget.com/rss.xml",
        "https://www.engadget.com",
    ),
    (
        "CleanTechnica",
        "rss",
        "https://cleantechnica.com/feed/",
        "https://cleantechnica.com",
    ),
    (
        "Electrek",
        "rss",
        "https://electrek.co/feed/",
        "https://electrek.co",
    ),
    (
        "SpaceNews",
        "rss",
        "https://spacenews.com/feed/",
        "https://spacenews.com",
    ),
]


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
                "name": name,
                "source_type": source_type,
                "endpoint_url": endpoint_url,
                "site_url": site_url,
                "is_active": True,
            }
            for name, source_type, endpoint_url, site_url in _NEW_SOURCES
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    for name, _, _, _ in _NEW_SOURCES:
        conn.execute(
            sa.text("DELETE FROM news_sources WHERE name = :name"),
            {"name": name},
        )
