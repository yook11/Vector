"""add ORNL news_source row + extend source_type enum to allow 'html'

Phase 3 PR 3-i-1。HTML listing 経路で取り込む初の Tier 1 ソース ORNL
(Oak Ridge National Laboratory) を追加する。

SourceType 拡張:

- ``ck_news_sources_source_type`` を drop して再作成し、新たに ``'html'``
  を許容する。SQLModel 側 (``app/models/news_source.py``) の ``SourceType``
  StrEnum も同 PR で ``HTML = 'html'`` を追加する。
- StrEnum は VARCHAR(20) 永続化のため Postgres enum type の alter は不要
  (CheckConstraint の差し替えのみで完結)。

attribution_label は U.S. Government work + ORNL/DOE クレジット表記
(短縮形を news_sources に格納し、UI 側で full credit に展開する想定)。

Revision ID: o15_add_ornl
Revises: o14_add_meti
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o15_add_ornl"
down_revision: str | None = "o14_add_meti"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CHECK = "source_type IN ('rss', 'api')"
_NEW_CHECK = "source_type IN ('rss', 'api', 'html')"

# (name, source_type, endpoint_url, site_url, attribution_label)
_NEW_SOURCES = [
    (
        "ORNL",
        "html",
        "https://www.ornl.gov/news",
        "https://www.ornl.gov",
        "ORNL · DOE",
    ),
]


def upgrade() -> None:
    op.drop_constraint("ck_news_sources_source_type", "news_sources", type_="check")
    op.create_check_constraint(
        "ck_news_sources_source_type",
        "news_sources",
        _NEW_CHECK,
    )

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

    op.drop_constraint("ck_news_sources_source_type", "news_sources", type_="check")
    op.create_check_constraint(
        "ck_news_sources_source_type",
        "news_sources",
        _OLD_CHECK,
    )
