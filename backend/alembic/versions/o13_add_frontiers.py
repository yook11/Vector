"""add Frontiers (4 journal) news_sources rows

Phase 3 PR 3-c-3。Frontiers Media (Open Access publisher) の 4 journal を
Pattern R Tier 1 ソースとして登録する。Frontiers in Artificial Intelligence /
Robotics and AI / Energy Research / Materials の 4 つを初版として採用。

全 journal 共通 license = CC BY 4.0 (Frontiers open access policy)、
attribution_label = ``"Frontiers in {Journal} · CC BY 4.0"``。

Revision ID: o13_add_frontiers
Revises: o12_add_cornell
Create Date: 2026-05-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o13_add_frontiers"
down_revision: str | None = "o12_add_cornell"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_TYPE = "rss"
_SITE_URL = "https://www.frontiersin.org/"
_JOURNALS = (
    {
        "name": "Frontiers in Artificial Intelligence",
        "endpoint_url": (
            "https://www.frontiersin.org/journals/artificial-intelligence/rss"
        ),
        "attribution_label": "Frontiers in Artificial Intelligence · CC BY 4.0",
    },
    {
        "name": "Frontiers in Robotics and AI",
        "endpoint_url": ("https://www.frontiersin.org/journals/robotics-and-ai/rss"),
        "attribution_label": "Frontiers in Robotics and AI · CC BY 4.0",
    },
    {
        "name": "Frontiers in Energy Research",
        "endpoint_url": ("https://www.frontiersin.org/journals/energy-research/rss"),
        "attribution_label": "Frontiers in Energy Research · CC BY 4.0",
    },
    {
        "name": "Frontiers in Materials",
        "endpoint_url": "https://www.frontiersin.org/journals/materials/rss",
        "attribution_label": "Frontiers in Materials · CC BY 4.0",
    },
)


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
                "name": j["name"],
                "source_type": _SOURCE_TYPE,
                "endpoint_url": j["endpoint_url"],
                "site_url": _SITE_URL,
                "is_active": True,
                "attribution_label": j["attribution_label"],
            }
            for j in _JOURNALS
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    for j in _JOURNALS:
        conn.execute(
            sa.text("DELETE FROM news_sources WHERE name = :name"),
            {"name": j["name"]},
        )
