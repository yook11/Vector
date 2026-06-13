"""rename stage 4 assessment tables to article state tables.

``in_scope_assessments`` / ``out_of_scope_assessments`` は Stage 4 の工程名ではなく、
永続化された記事状態として ``analyzed_articles`` / ``out_of_scope_articles`` に揃える。

public API / audit / observability の ``article_id`` は維持する。PR2 で扱う
``article_analysis_id`` / ``analysis_id`` の column rename もここでは行わない。

Revision ID: x5_analyzed_articles
Revises: x4_observed_article_payload
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "x5_analyzed_articles"
down_revision: str | None = "x4_observed_article_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: table rename + trigger body rewrite は contract。
MIGRATION_KIND = "contract"

_UP_CONSTRAINT_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("analyzed_articles", "article_analyses_pkey", "analyzed_articles_pkey"),
    (
        "analyzed_articles",
        "article_analyses_id_not_null",
        "analyzed_articles_id_not_null",
    ),
    (
        "analyzed_articles",
        "article_analyses_extraction_id_not_null",
        "analyzed_articles_curation_id_not_null",
    ),
    (
        "analyzed_articles",
        "article_analyses_translated_title_not_null",
        "analyzed_articles_translated_title_not_null",
    ),
    (
        "analyzed_articles",
        "article_analyses_summary_not_null",
        "analyzed_articles_summary_not_null",
    ),
    (
        "analyzed_articles",
        "article_analyses_reasoning_not_null",
        "analyzed_articles_investor_take_not_null",
    ),
    (
        "analyzed_articles",
        "article_analyses_analyzed_at_not_null",
        "analyzed_articles_analyzed_at_not_null",
    ),
    (
        "analyzed_articles",
        "article_analyses_category_id_not_null",
        "analyzed_articles_category_id_not_null",
    ),
    (
        "analyzed_articles",
        "uq_in_scope_assessments_curation_id",
        "uq_analyzed_articles_curation_id",
    ),
    (
        "analyzed_articles",
        "ck_in_scope_assessments_translated_title_not_empty",
        "ck_analyzed_articles_translated_title_not_empty",
    ),
    (
        "analyzed_articles",
        "ck_in_scope_assessments_summary_not_empty",
        "ck_analyzed_articles_summary_not_empty",
    ),
    (
        "analyzed_articles",
        "ck_in_scope_assessments_investor_take_not_empty",
        "ck_analyzed_articles_investor_take_not_empty",
    ),
    (
        "analyzed_articles",
        "fk_in_scope_assessments_curation_id",
        "fk_analyzed_articles_curation_id",
    ),
    (
        "analyzed_articles",
        "fk_in_scope_assessments_category_id",
        "fk_analyzed_articles_category_id",
    ),
    (
        "out_of_scope_articles",
        "article_rejections_pkey",
        "out_of_scope_articles_pkey",
    ),
    (
        "out_of_scope_articles",
        "article_rejections_id_not_null",
        "out_of_scope_articles_id_not_null",
    ),
    (
        "out_of_scope_articles",
        "article_rejections_extraction_id_not_null",
        "out_of_scope_articles_curation_id_not_null",
    ),
    (
        "out_of_scope_articles",
        "out_of_scope_assessments_translated_title_not_null",
        "out_of_scope_articles_translated_title_not_null",
    ),
    (
        "out_of_scope_articles",
        "out_of_scope_assessments_summary_not_null",
        "out_of_scope_articles_summary_not_null",
    ),
    (
        "out_of_scope_articles",
        "article_rejections_reasoning_not_null",
        "out_of_scope_articles_investor_take_not_null",
    ),
    (
        "out_of_scope_articles",
        "article_rejections_rejected_at_not_null",
        "out_of_scope_articles_rejected_at_not_null",
    ),
    (
        "out_of_scope_articles",
        "uq_out_of_scope_assessments_curation_id",
        "uq_out_of_scope_articles_curation_id",
    ),
    (
        "out_of_scope_articles",
        "ck_out_of_scope_assessments_translated_title_not_empty",
        "ck_out_of_scope_articles_translated_title_not_empty",
    ),
    (
        "out_of_scope_articles",
        "ck_out_of_scope_assessments_summary_not_empty",
        "ck_out_of_scope_articles_summary_not_empty",
    ),
    (
        "out_of_scope_articles",
        "ck_out_of_scope_assessments_investor_take_not_empty",
        "ck_out_of_scope_articles_investor_take_not_empty",
    ),
    (
        "out_of_scope_articles",
        "fk_out_of_scope_assessments_curation_id",
        "fk_out_of_scope_articles_curation_id",
    ),
)

_INDEX_RENAMES: tuple[tuple[str, str], ...] = (
    (
        "ix_in_scope_assessments_category_id_analyzed_at",
        "ix_analyzed_articles_category_id_analyzed_at",
    ),
    ("idx_in_scope_assessments_embedding", "idx_analyzed_articles_embedding"),
)


def _drop_old_exclusion_triggers() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_out_of_scope_assessments_no_in_scope "
        "ON out_of_scope_assessments;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_in_scope_assessments_no_out_of_scope "
        "ON in_scope_assessments;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_in_scope_for_out_of_scope();")
    op.execute("DROP FUNCTION IF EXISTS enforce_no_out_of_scope_for_in_scope();")


def _drop_new_exclusion_triggers() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_out_of_scope_articles_no_analyzed_article "
        "ON out_of_scope_articles;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_analyzed_articles_no_out_of_scope_article "
        "ON analyzed_articles;"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS enforce_no_analyzed_article_for_out_of_scope_article();"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS enforce_no_out_of_scope_article_for_analyzed_article();"
    )


def _create_new_exclusion_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_out_of_scope_article_for_analyzed_article()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM out_of_scope_articles
                WHERE curation_id = NEW.curation_id
            ) THEN
                RAISE EXCEPTION
                    'curation % already has an out_of_scope article',
                    NEW.curation_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_analyzed_articles_no_out_of_scope_article
        BEFORE INSERT OR UPDATE ON analyzed_articles
        FOR EACH ROW EXECUTE FUNCTION
            enforce_no_out_of_scope_article_for_analyzed_article();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_analyzed_article_for_out_of_scope_article()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM analyzed_articles
                WHERE curation_id = NEW.curation_id
            ) THEN
                RAISE EXCEPTION
                    'curation % already has an analyzed article',
                    NEW.curation_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_out_of_scope_articles_no_analyzed_article
        BEFORE INSERT OR UPDATE ON out_of_scope_articles
        FOR EACH ROW EXECUTE FUNCTION
            enforce_no_analyzed_article_for_out_of_scope_article();
        """
    )


def _create_old_exclusion_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_out_of_scope_for_in_scope()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM out_of_scope_assessments
                WHERE curation_id = NEW.curation_id
            ) THEN
                RAISE EXCEPTION
                    'curation % already has an out_of_scope assessment',
                    NEW.curation_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_in_scope_assessments_no_out_of_scope
        BEFORE INSERT OR UPDATE ON in_scope_assessments
        FOR EACH ROW EXECUTE FUNCTION enforce_no_out_of_scope_for_in_scope();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_in_scope_for_out_of_scope()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM in_scope_assessments
                WHERE curation_id = NEW.curation_id
            ) THEN
                RAISE EXCEPTION
                    'curation % already has an in_scope assessment',
                    NEW.curation_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_out_of_scope_assessments_no_in_scope
        BEFORE INSERT OR UPDATE ON out_of_scope_assessments
        FOR EACH ROW EXECUTE FUNCTION enforce_no_in_scope_for_out_of_scope();
        """
    )


def _rename_constraints(
    renames: tuple[tuple[str, str, str], ...],
    *,
    reverse: bool = False,
) -> None:
    for table, old, new in reversed(renames) if reverse else renames:
        source, target = (new, old) if reverse else (old, new)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conrelid = '{table}'::regclass
                      AND conname = '{source}'
                ) THEN
                    ALTER TABLE {table} RENAME CONSTRAINT {source} TO {target};
                END IF;
            END $$;
            """
        )


def _rename_indexes(
    renames: tuple[tuple[str, str], ...],
    *,
    reverse: bool = False,
) -> None:
    for old, new in reversed(renames) if reverse else renames:
        source, target = (new, old) if reverse else (old, new)
        op.execute(f"ALTER INDEX IF EXISTS {source} RENAME TO {target};")


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    _drop_old_exclusion_triggers()

    op.rename_table("in_scope_assessments", "analyzed_articles")
    op.rename_table("out_of_scope_assessments", "out_of_scope_articles")

    op.execute(
        "ALTER SEQUENCE IF EXISTS article_analyses_id_seq "
        "RENAME TO analyzed_articles_id_seq;"
    )
    op.execute(
        "ALTER SEQUENCE IF EXISTS in_scope_assessments_id_seq "
        "RENAME TO analyzed_articles_id_seq;"
    )
    op.execute(
        "ALTER SEQUENCE IF EXISTS article_rejections_id_seq "
        "RENAME TO out_of_scope_articles_id_seq;"
    )
    op.execute(
        "ALTER SEQUENCE IF EXISTS out_of_scope_assessments_id_seq "
        "RENAME TO out_of_scope_articles_id_seq;"
    )

    _rename_constraints(_UP_CONSTRAINT_RENAMES)
    _rename_indexes(_INDEX_RENAMES)
    _create_new_exclusion_triggers()


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    _drop_new_exclusion_triggers()

    _rename_indexes(_INDEX_RENAMES, reverse=True)
    _rename_constraints(_UP_CONSTRAINT_RENAMES, reverse=True)

    op.execute(
        "ALTER SEQUENCE IF EXISTS out_of_scope_articles_id_seq "
        "RENAME TO article_rejections_id_seq;"
    )
    op.execute(
        "ALTER SEQUENCE IF EXISTS analyzed_articles_id_seq "
        "RENAME TO article_analyses_id_seq;"
    )

    op.rename_table("out_of_scope_articles", "out_of_scope_assessments")
    op.rename_table("analyzed_articles", "in_scope_assessments")

    _create_old_exclusion_triggers()
