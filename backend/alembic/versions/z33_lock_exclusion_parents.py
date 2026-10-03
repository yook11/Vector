"""判定結果の排他トリガーで親の行をロックし、同時保存でも結果を1件に保つ。

対になる結果表（signal と noise、対象内と対象外）は、互いの行がないことを
トリガーで確認してから保存する。READ COMMITTED では相手の未確定の INSERT が
見えないため、同じ親に同時に保存すると両方が保存されていた。確認の前に共通の
親の行を FOR NO KEY UPDATE でロックし、同じ親への保存を順番に処理させる。
FOR UPDATE ではなくこの強さにするのは、他の子表の外部キー確認を待たせないため。

行ロックには UPDATE 権限が必要なため、分析ロールに親表の id 列だけを許可する。

Revision ID: z33_lock_exclusion_parents
Revises: z32_normalize_web_urls
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "z33_lock_exclusion_parents"
down_revision: str | None = "z32_normalize_web_urls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# トリガー関数の置き換えとGRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"

ROLE_NAME = "vector_article_analysis"

# 親の行をロックしてから相手表を確認する版。
_LOCKING_FUNCTIONS = (
    """
    CREATE OR REPLACE FUNCTION enforce_no_curation_noise_for_curation()
    RETURNS trigger AS $$
    BEGIN
        PERFORM 1 FROM analyzable_articles
        WHERE id = NEW.analyzable_article_id
        FOR NO KEY UPDATE;
        IF EXISTS (
            SELECT 1 FROM curation_noises
            WHERE analyzable_article_id = NEW.analyzable_article_id
        ) THEN
            RAISE EXCEPTION
                'article % already has a curation_noise',
                NEW.analyzable_article_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE OR REPLACE FUNCTION enforce_no_curation_for_curation_noise()
    RETURNS trigger AS $$
    BEGIN
        PERFORM 1 FROM analyzable_articles
        WHERE id = NEW.analyzable_article_id
        FOR NO KEY UPDATE;
        IF EXISTS (
            SELECT 1 FROM article_curations
            WHERE analyzable_article_id = NEW.analyzable_article_id
        ) THEN
            RAISE EXCEPTION
                'article % already has a curation',
                NEW.analyzable_article_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE OR REPLACE FUNCTION enforce_no_out_of_scope_article_for_analyzed_article()
    RETURNS trigger AS $$
    BEGIN
        PERFORM 1 FROM article_curations
        WHERE id = NEW.curation_id
        FOR NO KEY UPDATE;
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
    """,
    """
    CREATE OR REPLACE FUNCTION enforce_no_analyzed_article_for_out_of_scope_article()
    RETURNS trigger AS $$
    BEGIN
        PERFORM 1 FROM article_curations
        WHERE id = NEW.curation_id
        FOR NO KEY UPDATE;
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
    """,
)

# z32 時点の、親の行をロックしない版。
_PREVIOUS_FUNCTIONS = (
    """
    CREATE OR REPLACE FUNCTION enforce_no_curation_noise_for_curation()
    RETURNS trigger AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM curation_noises
            WHERE analyzable_article_id = NEW.analyzable_article_id
        ) THEN
            RAISE EXCEPTION
                'article % already has a curation_noise',
                NEW.analyzable_article_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE OR REPLACE FUNCTION enforce_no_curation_for_curation_noise()
    RETURNS trigger AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM article_curations
            WHERE analyzable_article_id = NEW.analyzable_article_id
        ) THEN
            RAISE EXCEPTION
                'article % already has a curation',
                NEW.analyzable_article_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
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
    """,
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
    """,
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    if not op.get_bind().scalar(
        text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :role)"),
        {"role": ROLE_NAME},
    ):
        raise RuntimeError("Create vector_article_analysis before migration")

    # 行ロックのためだけの権限で、id 以外の列は書き換えさせない。
    op.execute(
        "GRANT UPDATE (id) ON public.analyzable_articles, public.article_curations "
        "TO vector_article_analysis"
    )
    for function_sql in _LOCKING_FUNCTIONS:
        op.execute(function_sql)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    for function_sql in _PREVIOUS_FUNCTIONS:
        op.execute(function_sql)
    op.execute(
        "REVOKE UPDATE (id) ON public.analyzable_articles, public.article_curations "
        "FROM vector_article_analysis"
    )
