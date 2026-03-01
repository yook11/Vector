"""seed initial news sources (7 RSS feeds)

Revision ID: a3b4c5d6e7f9
Revises: a2b3c4d5e6f8
Create Date: 2026-03-01 00:02:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f9"
down_revision: Union[str, None] = "a2b3c4d5e6f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (name, source_type, feed_url, site_url, category_slug)
_SEED_SOURCES: list[tuple[str, str, str, str, str]] = [
    (
        "TechCrunch",
        "rss",
        "https://techcrunch.com/feed/",
        "https://techcrunch.com",
        "ai_ml",
    ),
    (
        "FierceBiotech",
        "rss",
        "https://www.fiercebiotech.com/rss/xml",
        "https://www.fiercebiotech.com",
        "biotech",
    ),
    (
        "BioPharma Dive",
        "rss",
        "https://www.biopharmadive.com/feeds/news/",
        "https://www.biopharmadive.com",
        "biotech",
    ),
    (
        "The Quantum Insider",
        "rss",
        "https://thequantuminsider.com/feed/",
        "https://thequantuminsider.com",
        "quantum",
    ),
    (
        "Cointelegraph",
        "rss",
        "https://cointelegraph.com/rss",
        "https://cointelegraph.com",
        "fintech",
    ),
    (
        "Yahoo Finance",
        "rss",
        "https://finance.yahoo.com/news/rssindex",
        "https://finance.yahoo.com",
        "fintech",
    ),
    (
        "ITmedia",
        "rss",
        "https://rss.itmedia.co.jp/rss/2.0/itmedia_all.xml",
        "https://www.itmedia.co.jp",
        "ai_ml",
    ),
]


def upgrade() -> None:
    conn = op.get_bind()

    # Resolve category slugs to IDs
    slug_to_id: dict[str, int] = {}
    needed_slugs = {s[4] for s in _SEED_SOURCES}
    for slug in needed_slugs:
        result = conn.execute(
            sa.text("SELECT id FROM keyword_categories WHERE slug = :slug"),
            {"slug": slug},
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError(
                f"keyword_categories slug '{slug}' not found. "
                "Ensure the category seed migration has been applied."
            )
        slug_to_id[slug] = row[0]

    # Insert news sources
    sources_table = sa.table(
        "news_sources",
        sa.column("name", sa.String),
        sa.column("source_type", sa.String),
        sa.column("feed_url", sa.String),
        sa.column("site_url", sa.String),
        sa.column("category_id", sa.Integer),
        sa.column("is_active", sa.Boolean),
        sa.column("fetch_interval_minutes", sa.Integer),
    )
    op.bulk_insert(
        sources_table,
        [
            {
                "name": name,
                "source_type": source_type,
                "feed_url": feed_url,
                "site_url": site_url,
                "category_id": slug_to_id[category_slug],
                "is_active": True,
                "fetch_interval_minutes": 720,
            }
            for name, source_type, feed_url, site_url, category_slug in _SEED_SOURCES
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    for name, _, feed_url, _, _ in _SEED_SOURCES:
        conn.execute(
            sa.text("DELETE FROM news_sources WHERE feed_url = :feed_url"),
            {"feed_url": feed_url},
        )
