"""add Anthropic news_sources row

Phase 3 PR 3-d-4。Anthropic news を Tier 1 ソースとして news_sources に
登録する。RSS 不在 (`/rss.xml` `/feed` `/news/rss.xml` 全て 404) のため
``BaseSitemapFetcher`` 経由 (sitemap.xml → URL 列挙 → ``extract_html_body``)。

attribution_label は Anthropic 公式の標準利用規約相当文言が無いため source
name のみ。

source_type は ``"rss"`` を流用する: SourceType enum の CHECK constraint が
``IN ('rss', 'api')`` で固定されており、新値 ``'sitemap'`` の追加は別 PR
での schema 変更を要する。本フィールドは現行コードベースで dispatch logic
に使われていない (``FETCHERS`` は ``name`` で引く) ため、ラベル不正確は
記録上の表示問題に留まり、運用に影響しない (PR 3-i-1 ORNL HTML scrape と
合わせて enum 拡張する想定)。

Revision ID: o3_add_anthropic
Revises: o2_add_mext_mic
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o3_add_anthropic"
down_revision: str | None = "o2_add_mext_mic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, source_type, endpoint_url, site_url, attribution_label)
_NEW_SOURCES = [
    (
        "Anthropic",
        "rss",
        "https://www.anthropic.com/sitemap.xml",
        "https://www.anthropic.com",
        "Anthropic",
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
