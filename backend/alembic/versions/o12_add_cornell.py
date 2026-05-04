"""add Cornell Chronicle news_sources row

Phase 3 PR 3-e。Cornell Chronicle (`https://news.cornell.edu/`) を
Pattern H + 6 taxonomy term feed 巡回の Tier 1 ソースとして登録する。

attribution_label = "Cornell Chronicle"。endpoint_url は代表値として
AI feed (term/24043) を採用する (実 fetch は fetcher 内 ``FEEDS`` ClassVar
の 6 URL を順次巡回)。

Revision ID: o12_add_cornell
Revises: o11_add_other_category
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o12_add_cornell"
down_revision: str | None = "o11_add_other_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "Cornell Chronicle"
_SOURCE_TYPE = "rss"
_ENDPOINT_URL = "https://news.cornell.edu/taxonomy/term/24043/feed"
_SITE_URL = "https://news.cornell.edu/"
_ATTRIBUTION_LABEL = "Cornell Chronicle"


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
                "name": _NAME,
                "source_type": _SOURCE_TYPE,
                "endpoint_url": _ENDPOINT_URL,
                "site_url": _SITE_URL,
                "is_active": True,
                "attribution_label": _ATTRIBUTION_LABEL,
            }
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM news_sources WHERE name = :name"),
        {"name": _NAME},
    )
