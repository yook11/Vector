"""add ESA/Hubble + ESA/Webb news_sources rows

Phase 3 PR 3-b。ESA/Hubble + ESA/Webb (Djangoplicity 規格 RSS) を Tier 1
ソースとして news_sources に登録する。

両者とも ESA + NASA 共同運用、image credit は ESA 公式の RSS feed
であるため "ESA/Hubble" / "ESA/Webb" を attribution_label として採用。

Revision ID: o6_add_esa
Revises: o5_add_cf_deepmind
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o6_add_esa"
down_revision: str | None = "o5_add_cf_deepmind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, source_type, endpoint_url, site_url, attribution_label)
_NEW_SOURCES = [
    (
        "ESA/Hubble",
        "rss",
        "https://esahubble.org/news/feed/",
        "https://esahubble.org",
        "ESA/Hubble",
    ),
    (
        "ESA/Webb",
        "rss",
        "https://esawebb.org/news/feed/",
        "https://esawebb.org",
        "ESA/Webb",
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
