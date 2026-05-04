"""add METI news_source row

Phase 3 PR 3-h-2。経済産業省 (METI) の Atom feed を Tier 1 ソースとして
news_sources に登録する。

attribution_label は政府標準利用規約のサンプル文言に翻訳マークを付与した
形式 (CC BY 4.0 §3(a)(1)(B) 互換): 「出典：経済産業省ホームページ
(https://www.meti.go.jp/) を翻訳」。

Revision ID: o14_add_meti
Revises: q1_pipeline_events
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o14_add_meti"
down_revision: str | None = "q1_pipeline_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, source_type, endpoint_url, site_url, attribution_label)
_NEW_SOURCES = [
    (
        "METI",
        "rss",
        "https://www.meti.go.jp/ml_index_release_atom.xml",
        "https://www.meti.go.jp",
        "出典：経済産業省ホームページ（https://www.meti.go.jp/）を翻訳",
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
