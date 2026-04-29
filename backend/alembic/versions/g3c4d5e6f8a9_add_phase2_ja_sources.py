"""Phase 2: 日本語 RSS 3 ソース投入 (MONOist / EE Times Japan / ITmedia NEWS)。

`specs/source-strategy/roadmap.md` の Phase 2 着手分割 PR-3。
全て BaseRssFetcher 継承のみで実装可能な構造同型ソース。

Revision ID: g3c4d5e6f8a9
Revises: f0a2b3c4d5e7
Create Date: 2026-04-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "g3c4d5e6f8a9"
down_revision: Union[str, None] = "f0a2b3c4d5e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (name, source_type, endpoint_url, site_url)
_NEW_SOURCES = [
    (
        "MONOist",
        "rss",
        "https://rss.itmedia.co.jp/rss/2.0/monoist.xml",
        "https://monoist.itmedia.co.jp",
    ),
    (
        "EE Times Japan",
        "rss",
        "https://rss.itmedia.co.jp/rss/2.0/eetimes.xml",
        "https://eetimes.itmedia.co.jp",
    ),
    (
        "ITmedia NEWS",
        "rss",
        "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
        "https://www.itmedia.co.jp/news/",
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
