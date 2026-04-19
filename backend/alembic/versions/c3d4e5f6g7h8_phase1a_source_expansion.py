"""Phase 1a ソース拡張: 5ソース無効化 + 8ソース追加。

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-04-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6g7h8"
down_revision: Union[str, None] = "b2c3d4e5f6g7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEACTIVATE_NAMES = [
    "Yahoo Finance",
    "Cointelegraph",
    "Alpha Vantage",
    "BioPharma Dive",
    "ITmedia",
]

# (name, source_type, endpoint_url, site_url)
_NEW_SOURCES = [
    (
        "IEEE Spectrum",
        "rss",
        "https://spectrum.ieee.org/feeds/feed.rss",
        "https://spectrum.ieee.org",
    ),
    (
        "NASA",
        "rss",
        "https://www.nasa.gov/news-release/feed/",
        "https://www.nasa.gov",
    ),
    (
        "Microsoft Research",
        "rss",
        "https://www.microsoft.com/en-us/research/feed/",
        "https://www.microsoft.com/en-us/research",
    ),
    (
        "Krebs on Security",
        "rss",
        "https://krebsonsecurity.com/feed/",
        "https://krebsonsecurity.com",
    ),
    (
        "VentureBeat",
        "rss",
        "https://venturebeat.com/feed/",
        "https://venturebeat.com",
    ),
    (
        "Spaceflight Now",
        "rss",
        "https://spaceflightnow.com/feed/",
        "https://spaceflightnow.com",
    ),
    (
        "ITmedia AI+",
        "rss",
        "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
        "https://www.itmedia.co.jp",
    ),
    (
        "JPCERT/CC",
        "rss",
        "https://www.jpcert.or.jp/rss/jpcert.rdf",
        "https://www.jpcert.or.jp",
    ),
]


def upgrade() -> None:
    conn = op.get_bind()

    # 5ソースを無効化
    for name in _DEACTIVATE_NAMES:
        conn.execute(
            sa.text(
                "UPDATE news_sources SET is_active = false, updated_at = now() "
                "WHERE name = :name"
            ),
            {"name": name},
        )

    # 8ソースを追加
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

    # 8ソースを削除
    for name, _, _, _ in _NEW_SOURCES:
        conn.execute(
            sa.text("DELETE FROM news_sources WHERE name = :name"),
            {"name": name},
        )

    # 5ソースを再有効化
    for name in _DEACTIVATE_NAMES:
        conn.execute(
            sa.text(
                "UPDATE news_sources SET is_active = true, updated_at = now() "
                "WHERE name = :name"
            ),
            {"name": name},
        )
