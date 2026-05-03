"""add NIST + NSF news_sources rows

Phase 3 PR 3-a。NIST (National Institute of Standards and Technology) と
NSF (National Science Foundation) の RSS feed を Tier 1 ソースとして
news_sources に登録する。

attribution_label は両者とも 17 U.S.C. §105 (Public Domain) に基づき、
シンプルな組織名のみ。

Revision ID: o4_add_nist_nsf
Revises: o3_add_anthropic
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o4_add_nist_nsf"
down_revision: str | None = "o3_add_anthropic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, source_type, endpoint_url, site_url, attribution_label)
_NEW_SOURCES = [
    (
        "NIST",
        "rss",
        "https://www.nist.gov/news-events/news/rss.xml",
        "https://www.nist.gov",
        "NIST",
    ),
    (
        "NSF",
        "rss",
        "https://www.nsf.gov/rss/rss_www_news.xml",
        "https://www.nsf.gov",
        "NSF",
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
        sa.column("attribution_label", sa.Text),
    )
    op.bulk_insert(
        sources_table,
        [
            {
                "name": name,
                "source_type": stype,
                "endpoint_url": endpoint,
                "site_url": site,
                "is_active": True,
                "attribution_label": label,
            }
            for name, stype, endpoint, site, label in _NEW_SOURCES
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    for name, *_ in _NEW_SOURCES:
        conn.execute(
            sa.text("DELETE FROM news_sources WHERE name = :name"),
            {"name": name},
        )
