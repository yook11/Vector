"""add eLife news_sources row

Phase 3 PR 3-c-2。eLife (`https://elifesciences.org/rss/recent.xml`) を
Pattern R (description 本文 1500 字程度) で取り込む Tier 1 ソースとして登録する。

attribution_label = "eLife"。CC BY 4.0 で全件統一 (open access policy)。

Revision ID: o8_add_elife
Revises: o7_add_openai_hf
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o8_add_elife"
down_revision: str | None = "o7_add_openai_hf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "eLife"
_SOURCE_TYPE = "rss"
_ENDPOINT_URL = "https://elifesciences.org/rss/recent.xml"
_SITE_URL = "https://elifesciences.org/"
_ATTRIBUTION_LABEL = "eLife"


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
