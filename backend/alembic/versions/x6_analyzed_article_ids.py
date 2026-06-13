"""rename analyzed article id fields.

``analyzed_articles.id`` を指す internal / persisted key を
``analyzed_article_id`` に統一する。

Revision ID: x6_analyzed_article_ids
Revises: x5_analyzed_articles
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "x6_analyzed_article_ids"
down_revision: str | None = "x5_analyzed_articles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: column rename + JSONB UPDATE を含むため contract。
MIGRATION_KIND = "contract"


def _rename_key_sql(*, from_key: str, to_key: str) -> str:
    rename_expr = (
        f"(elem - '{from_key}') || "
        f"jsonb_build_object('{to_key}', elem->'{from_key}')"
    )
    return (
        "UPDATE weekly_briefings wb SET key_articles = sub.key_articles"
        " FROM ("
        "  SELECT t.id, jsonb_agg("
        f"   CASE WHEN jsonb_typeof(elem) = 'object' AND elem ? '{from_key}'"
        f"    THEN {rename_expr}"
        "    ELSE elem END"
        "   ORDER BY ord"
        "  ) AS key_articles"
        "  FROM weekly_briefings t,"
        "  LATERAL jsonb_array_elements(t.key_articles)"
        "  WITH ORDINALITY AS arr(elem, ord)"
        "  WHERE jsonb_typeof(t.key_articles) = 'array'"
        "  GROUP BY t.id"
        f"  HAVING bool_or(jsonb_typeof(elem) = 'object' AND elem ? '{from_key}')"
        " ) sub"
        " WHERE wb.id = sub.id"
    )


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.alter_column(
        "watchlist_entries",
        "article_analysis_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        new_column_name="analyzed_article_id",
    )
    op.execute(
        "ALTER TABLE watchlist_entries RENAME CONSTRAINT "
        "fk_watchlist_entries_article_analysis_id "
        "TO fk_watchlist_entries_analyzed_article_id;"
    )

    op.alter_column(
        "embedding_backfill_exclusions",
        "analysis_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        new_column_name="analyzed_article_id",
    )
    op.execute(
        "ALTER TABLE embedding_backfill_exclusions RENAME CONSTRAINT "
        "embedding_backfill_exclusions_analysis_id_fkey "
        "TO embedding_backfill_exclusions_analyzed_article_id_fkey;"
    )

    op.execute(
        _rename_key_sql(
            from_key="assessment_id",
            to_key="analyzed_article_id",
        )
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.alter_column(
        "watchlist_entries",
        "analyzed_article_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        new_column_name="article_analysis_id",
    )
    op.execute(
        "ALTER TABLE watchlist_entries RENAME CONSTRAINT "
        "fk_watchlist_entries_analyzed_article_id "
        "TO fk_watchlist_entries_article_analysis_id;"
    )

    op.alter_column(
        "embedding_backfill_exclusions",
        "analyzed_article_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        new_column_name="analysis_id",
    )
    op.execute(
        "ALTER TABLE embedding_backfill_exclusions RENAME CONSTRAINT "
        "embedding_backfill_exclusions_analyzed_article_id_fkey "
        "TO embedding_backfill_exclusions_analysis_id_fkey;"
    )

    op.execute(
        _rename_key_sql(
            from_key="analyzed_article_id",
            to_key="assessment_id",
        )
    )
