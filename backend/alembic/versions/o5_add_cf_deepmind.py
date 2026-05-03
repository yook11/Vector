"""add Cloudflare Blog + Google DeepMind news_sources rows

Phase 3 PR 3-d-1。Cloudflare Blog (Pattern R RSS) と Google DeepMind
(Pattern H RSS) を Tier 1 ソースとして news_sources に登録する。

attribution_label は Phase 2 法務リサーチ確定値を採用。

Revision ID: o5_add_cf_deepmind
Revises: o4_add_nist_nsf
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o5_add_cf_deepmind"
down_revision: str | None = "o4_add_nist_nsf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, source_type, endpoint_url, site_url, attribution_label)
_NEW_SOURCES = [
    (
        "The Cloudflare Blog",
        "rss",
        "https://blog.cloudflare.com/rss/",
        "https://blog.cloudflare.com",
        "The Cloudflare Blog",
    ),
    (
        "Google DeepMind",
        "rss",
        "https://deepmind.google/blog/rss.xml",
        "https://deepmind.google/blog/",
        "Google DeepMind",
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
