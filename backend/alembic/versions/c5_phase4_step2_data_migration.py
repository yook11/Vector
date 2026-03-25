"""Phase 4 Step 2: Migrate data to new columns and article_analyses table.

- 2-A: Copy news_articles columns (title_original -> original_title, etc.)
- 2-B: INSERT into article_analyses from analyses + analysis_translations
        + news_articles.embedding, with impact_score -> impact_level mapping

Revision ID: c5a1b2c3d4e5
Revises: c4a1b2c3d4e5
Create Date: 2026-03-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5a1b2c3d4e5"
down_revision: str | None = "c4a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 2-A. Copy news_articles columns ---
    op.execute(
        sa.text(
            "UPDATE news_articles SET "
            "original_title   = title_original, "
            "original_url     = url, "
            "original_content = content, "
            "news_source_id   = source_id, "
            "created_at       = fetched_at"
        )
    )

    # --- 2-B. Migrate analyses -> article_analyses ---
    # Combines: analyses + ai_models + analysis_translations + news_articles.embedding
    # impact_score (1-10) -> impact_level (low/medium/high/critical):
    #   4-7 = low, 3/8 = medium, 2/9 = high, 1/10 = critical
    op.execute(
        sa.text(
            "INSERT INTO article_analyses ("
            "  news_article_id, translated_title, summary,"
            "  impact_level, reasoning, ai_model, analyzed_at,"
            "  embedding, embedding_model"
            ") "
            "SELECT "
            "  a.news_article_id, "
            "  t.title, "
            "  t.summary, "
            "  CASE "
            "    WHEN a.impact_score BETWEEN 4 AND 7 THEN 'low' "
            "    WHEN a.impact_score IN (3, 8)       THEN 'medium' "
            "    WHEN a.impact_score IN (2, 9)       THEN 'high' "
            "    WHEN a.impact_score IN (1, 10)      THEN 'critical' "
            "  END, "
            "  a.reasoning, "
            "  m.name, "
            "  a.analyzed_at, "
            "  na.embedding, "
            "  CASE WHEN na.embedding IS NOT NULL "
            "    THEN 'text-embedding-004' ELSE NULL END "
            "FROM analyses a "
            "JOIN ai_models m ON m.id = a.ai_model_id "
            "JOIN analysis_translations t "
            "  ON t.analysis_id = a.id AND t.locale = 'ja' "
            "JOIN news_articles na ON na.id = a.news_article_id"
        )
    )


def downgrade() -> None:
    # --- Reverse 2-B: truncate article_analyses ---
    op.execute(sa.text("TRUNCATE article_analyses"))

    # --- Reverse 2-A: clear new columns ---
    op.execute(
        sa.text(
            "UPDATE news_articles SET "
            "original_title   = NULL, "
            "original_url     = NULL, "
            "original_content = NULL, "
            "news_source_id   = NULL, "
            "created_at       = NULL"
        )
    )
