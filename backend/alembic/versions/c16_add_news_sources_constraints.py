"""Add constraints to news_sources.

- Composite unique on (name, source_type)
- CHECK site_url starts with http(s)://
- CHECK endpoint_url starts with http(s)://

Note: ck_news_sources_source_type already exists from c3 migration.

Revision ID: c16a1b2c3d4e
Revises: c15a1b2c3d4e
Create Date: 2026-03-28
"""

from alembic import op

revision = "c16a1b2c3d4e"
down_revision = "c15a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_news_sources_name_source_type",
        "news_sources",
        ["name", "source_type"],
    )
    op.create_check_constraint(
        "ck_news_sources_site_url_scheme",
        "news_sources",
        "site_url ~ '^https?://.+'",
    )
    op.create_check_constraint(
        "ck_news_sources_endpoint_url_scheme",
        "news_sources",
        "endpoint_url ~ '^https?://.+'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_news_sources_endpoint_url_scheme", "news_sources", type_="check"
    )
    op.drop_constraint(
        "ck_news_sources_site_url_scheme", "news_sources", type_="check"
    )
    op.drop_constraint(
        "uq_news_sources_name_source_type", "news_sources", type_="unique"
    )
