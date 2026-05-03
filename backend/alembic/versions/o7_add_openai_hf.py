"""add OpenAI + Hugging Face Blog news_sources rows

Phase 3 PR 3-d-2。OpenAI 公式 news (`https://openai.com/news/rss.xml`) と
Hugging Face Blog (`https://huggingface.co/blog/feed.xml`) を Pattern H
で取り込む Tier 1 ソースとして登録する。

両者とも組織発信、attribution は `OpenAI` / `Hugging Face` 単独。

Revision ID: o7_add_openai_hf
Revises: o6_add_esa
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o7_add_openai_hf"
down_revision: str | None = "o6_add_esa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, source_type, endpoint_url, site_url, attribution_label)
_NEW_SOURCES = [
    (
        "OpenAI",
        "rss",
        "https://openai.com/news/rss.xml",
        "https://openai.com/news/",
        "OpenAI",
    ),
    (
        "Hugging Face",
        "rss",
        "https://huggingface.co/blog/feed.xml",
        "https://huggingface.co/blog",
        "Hugging Face",
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
