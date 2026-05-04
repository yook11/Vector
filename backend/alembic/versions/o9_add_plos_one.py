"""add PLOS ONE news_sources row

Phase 3 PR 3-c-1。PLOS ONE (`https://journals.plos.org/plosone/feed/atom`) を
Atom 1.0 Pattern R で取り込む Tier 1 ソースとして登録する。Tier 1 で唯一の
Atom feed。CC BY 4.0 全件統一。

attribution_label = "PLOS ONE"。

Revision ID: o9_add_plos_one
Revises: o8_add_elife
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o9_add_plos_one"
down_revision: str | None = "o8_add_elife"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "PLOS ONE"
_SOURCE_TYPE = "rss"
_ENDPOINT_URL = "https://journals.plos.org/plosone/feed/atom"
_SITE_URL = "https://journals.plos.org/plosone/"
_ATTRIBUTION_LABEL = "PLOS ONE"


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
