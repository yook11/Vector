"""Refactor news_sources table — Phase 3.

- Merge feed_url + api_endpoint into endpoint_url (NOT NULL, UNIQUE)
- Shrink name VARCHAR(200) -> VARCHAR(50)
- Make site_url NOT NULL
- Drop pipeline columns: etag, last_modified_header, fetch_interval_minutes,
  next_fetch_at, last_fetched_at, consecutive_errors, last_error_message
- Drop feed_url, api_endpoint
- Drop obsolete CHECK constraints and indexes

Revision ID: c3a1b2c3d4e5
Revises: c2a1b2c3d4e5
Create Date: 2026-03-25 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3a1b2c3d4e5"
down_revision: Union[str, None] = "c2a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. Add endpoint_url (temporarily nullable for data migration) ---
    op.add_column(
        "news_sources",
        sa.Column("endpoint_url", sa.String(length=2048), nullable=True),
    )

    # --- 2. Migrate data: feed_url / api_endpoint -> endpoint_url ---
    conn = op.get_bind()
    # RSS sources: copy feed_url
    conn.execute(
        sa.text(
            "UPDATE news_sources SET endpoint_url = feed_url "
            "WHERE source_type = 'rss' AND feed_url IS NOT NULL"
        )
    )
    # API sources: map known identifiers to real URLs
    conn.execute(
        sa.text(
            "UPDATE news_sources SET endpoint_url = 'https://hn.algolia.com/api/v1/search_by_date' "
            "WHERE api_endpoint = 'hacker-news'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE news_sources SET endpoint_url = 'https://www.alphavantage.co/query' "
            "WHERE api_endpoint = 'alpha-vantage'"
        )
    )
    # Safety: any remaining rows get a unique placeholder using their PK
    conn.execute(
        sa.text(
            "UPDATE news_sources "
            "SET endpoint_url = 'https://unknown.example.com/' || id::text "
            "WHERE endpoint_url IS NULL"
        )
    )

    # --- 3. Make endpoint_url NOT NULL + UNIQUE ---
    op.alter_column("news_sources", "endpoint_url", nullable=False)
    op.create_unique_constraint(
        "uq_news_sources_endpoint_url", "news_sources", ["endpoint_url"]
    )

    # --- 4. Handle site_url NULL -> NOT NULL ---
    # Fill any NULL site_url with endpoint_url as fallback
    conn.execute(
        sa.text(
            "UPDATE news_sources SET site_url = endpoint_url WHERE site_url IS NULL"
        )
    )
    op.alter_column("news_sources", "site_url", nullable=False)

    # --- 5. Shrink name VARCHAR(200) -> VARCHAR(50) ---
    # Truncate any names longer than 50 chars (defensive)
    conn.execute(
        sa.text(
            "UPDATE news_sources SET name = LEFT(name, 50) "
            "WHERE char_length(name) > 50"
        )
    )
    op.alter_column(
        "news_sources",
        "name",
        type_=sa.String(length=50),
        existing_type=sa.String(length=200),
        existing_nullable=False,
    )

    # --- 6. Add new CHECK constraint per spec ---
    # Both bounds use trimmed length to match spec: "trim後1-50文字"
    op.create_check_constraint(
        "ck_news_sources_name_length",
        "news_sources",
        "char_length(trim(name)) >= 1 AND char_length(trim(name)) <= 50",
    )

    # --- 7. Drop obsolete constraints and indexes ---
    op.drop_constraint("ck_news_sources_type_fields", "news_sources", type_="check")
    op.drop_constraint("ck_news_sources_interval_range", "news_sources", type_="check")
    op.drop_index("idx_sources_active_next_fetch", table_name="news_sources")
    # PostgreSQL auto-generated unique constraint name for feed_url column
    op.drop_constraint("news_sources_feed_url_key", "news_sources", type_="unique")

    # --- 8. Drop columns ---
    columns_to_drop = [
        "feed_url",
        "api_endpoint",
        "etag",
        "last_modified_header",
        "fetch_interval_minutes",
        "next_fetch_at",
        "last_fetched_at",
        "consecutive_errors",
        "last_error_message",
    ]
    for col in columns_to_drop:
        op.drop_column("news_sources", col)


def downgrade() -> None:
    # --- Restore dropped columns ---
    op.add_column(
        "news_sources",
        sa.Column("feed_url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "news_sources",
        sa.Column("api_endpoint", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "news_sources",
        sa.Column("etag", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "news_sources",
        sa.Column("last_modified_header", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "news_sources",
        sa.Column(
            "fetch_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="720",
        ),
    )
    op.add_column(
        "news_sources",
        sa.Column("next_fetch_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "news_sources",
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "news_sources",
        sa.Column(
            "consecutive_errors",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "news_sources",
        sa.Column("last_error_message", sa.Text(), nullable=True),
    )

    # --- Restore data: endpoint_url -> feed_url / api_endpoint ---
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE news_sources SET feed_url = endpoint_url "
            "WHERE source_type = 'rss'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE news_sources SET api_endpoint = 'hacker-news' "
            "WHERE endpoint_url LIKE '%algolia.com%'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE news_sources SET api_endpoint = 'alpha-vantage' "
            "WHERE endpoint_url LIKE '%alphavantage.co%'"
        )
    )

    # --- Restore constraints and indexes ---
    op.create_unique_constraint(
        "news_sources_feed_url_key", "news_sources", ["feed_url"]
    )
    op.create_index(
        "idx_sources_active_next_fetch",
        "news_sources",
        ["next_fetch_at"],
        postgresql_where=sa.text("is_active = TRUE"),
    )
    op.create_check_constraint(
        "ck_news_sources_interval_range",
        "news_sources",
        "fetch_interval_minutes BETWEEN 15 AND 1440",
    )
    op.create_check_constraint(
        "ck_news_sources_type_fields",
        "news_sources",
        "(source_type = 'rss' AND feed_url IS NOT NULL) "
        "OR (source_type = 'api' AND api_endpoint IS NOT NULL)",
    )

    # --- Drop new constraints and column ---
    op.drop_constraint("ck_news_sources_name_length", "news_sources", type_="check")
    op.drop_constraint("uq_news_sources_endpoint_url", "news_sources", type_="unique")
    op.drop_column("news_sources", "endpoint_url")

    # --- Restore name to VARCHAR(200) ---
    op.alter_column(
        "news_sources",
        "name",
        type_=sa.String(length=200),
        existing_type=sa.String(length=50),
        existing_nullable=False,
    )

    # --- Restore site_url to nullable ---
    op.alter_column("news_sources", "site_url", nullable=True)
