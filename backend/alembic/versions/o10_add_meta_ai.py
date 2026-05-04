"""add Meta AI news_sources row

Phase 3 PR 3-d-3。Meta Newsroom (`https://about.fb.com/news/feed/`) を
Pattern R + AI tag フィルタで取り込む Tier 1 ソースとして登録する。
``ai.meta.com`` は専用 RSS / sitemap 一切提供なしのため代替経路。

attribution_label = "Meta Newsroom"。AI tag フィルタは fetcher 側で実施
(business critical、Newsroom は WhatsApp / Threads / Sustainability 等
全社混在で約 60% が非 AI 記事)。

Revision ID: o10_add_meta_ai
Revises: o9_add_plos_one
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o10_add_meta_ai"
down_revision: str | None = "o9_add_plos_one"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "Meta AI"
_SOURCE_TYPE = "rss"
_ENDPOINT_URL = "https://about.fb.com/news/feed/"
_SITE_URL = "https://about.fb.com/"
_ATTRIBUTION_LABEL = "Meta Newsroom"


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
