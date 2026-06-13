"""rename incomplete_articles staged_attributes to observed_article.

``incomplete_articles.staged_attributes`` は Stage 1 で観測できた記事事実
``ObservedArticle`` の JSONB payload なので、DB column を
``observed_article`` に揃える。

既存 JSONB データは変更せず、column rename のみを行う。旧 backend は
``staged_attributes`` を参照し、新 backend は ``observed_article`` を参照するため、
deploy は stop-the-world 前提。

Revision ID: x4_observed_article_payload
Revises: x3_analyzable_article_fks
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "x4_observed_article_payload"
down_revision: str | None = "x3_analyzable_article_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: alter_column(new_column_name=...) は contract。
MIGRATION_KIND = "contract"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    op.alter_column(
        "incomplete_articles",
        "staged_attributes",
        new_column_name="observed_article",
        existing_type=postgresql.JSONB(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    op.alter_column(
        "incomplete_articles",
        "observed_article",
        new_column_name="staged_attributes",
        existing_type=postgresql.JSONB(),
        existing_nullable=False,
    )
