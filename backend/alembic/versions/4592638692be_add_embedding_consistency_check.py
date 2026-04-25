"""add embedding consistency CHECK constraint

Add ``ck_article_analyses_embedding_consistency`` so that ``embedding`` and
``embedding_model`` are either both NULL (unembedded) or both NOT NULL
(embedded). This enforces the Embedding aggregate's structural guarantee at
the DB layer alongside the domain ``EmbeddingRepository._to_domain`` defense.

Pre-flight check (run on production-like DB before deploying):

    SELECT count(*) FROM article_analyses
    WHERE (embedding IS NULL) != (embedding_model IS NULL);

If 0 rows: this migration adds the constraint cleanly.
If >0 rows: the upgrade backfills ``embedding_model = 'unknown:legacy'`` for
rows where ``embedding IS NOT NULL AND embedding_model IS NULL`` before
adding the constraint. The reverse case (``embedding IS NULL AND
embedding_model IS NOT NULL``) clears the orphan ``embedding_model``.

Revision ID: 4592638692be
Revises: 4d16d9b326a0
Create Date: 2026-04-25 13:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "4592638692be"
down_revision: str | None = "4d16d9b326a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    # Backfill: heal any pre-existing partial-NULL rows so the CHECK can be
    # added without violating existing data. Idempotent.
    op.execute(
        """
        UPDATE article_analyses
        SET embedding_model = 'unknown:legacy'
        WHERE embedding IS NOT NULL AND embedding_model IS NULL
        """
    )
    op.execute(
        """
        UPDATE article_analyses
        SET embedding_model = NULL
        WHERE embedding IS NULL AND embedding_model IS NOT NULL
        """
    )

    op.create_check_constraint(
        "ck_article_analyses_embedding_consistency",
        "article_analyses",
        "(embedding IS NULL AND embedding_model IS NULL) "
        "OR (embedding IS NOT NULL AND embedding_model IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_constraint(
        "ck_article_analyses_embedding_consistency",
        "article_analyses",
        type_="check",
    )
