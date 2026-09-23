"""記事単位AI分析ロールに分析結果の保存と監査・Outboxの追加を許可する。

Revision ID: z25_grant_article_analysis
Revises: z24_auth_cleanup_grants
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "z25_grant_article_analysis"
down_revision: str | None = "z24_auth_cleanup_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"

ROLE_NAME = "vector_article_analysis"
_SEQUENCE_TABLES = (
    "article_curations",
    "curation_noises",
    "analyzed_articles",
    "out_of_scope_articles",
    "pipeline_events",
)


def _sequence_grants(statement: str) -> str:
    tables = ", ".join(f"'{table}'" for table in _SEQUENCE_TABLES)
    return f"""
        DO $$
        DECLARE
          t text;
          seq text;
        BEGIN
          FOREACH t IN ARRAY ARRAY[{tables}]
          LOOP
            seq := pg_get_serial_sequence('public.' || t, 'id');
            IF seq IS NULL THEN
              RAISE EXCEPTION 'id sequence of % is missing', t;
            END IF;
            EXECUTE format('{statement}', seq);
          END LOOP;
        END $$;
    """


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    if not op.get_bind().scalar(
        text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :role)"),
        {"role": ROLE_NAME},
    ):
        raise RuntimeError("Create vector_article_analysis before migration")

    op.execute("GRANT USAGE ON SCHEMA public TO vector_article_analysis")
    op.execute(
        "GRANT SELECT ON public.analyzable_articles, public.categories "
        "TO vector_article_analysis"
    )
    op.execute(
        "GRANT SELECT, INSERT ON public.article_curations, public.curation_noises, "
        "public.analyzed_articles, public.out_of_scope_articles "
        "TO vector_article_analysis"
    )
    # 保存済みの分析結果は書き換えさせず、embeddingの保存と行ロックだけを許す。
    op.execute(
        "GRANT UPDATE (embedding) ON public.analyzed_articles "
        "TO vector_article_analysis"
    )
    # 監査とOutboxは追加だけとし、SELECTはORMがRETURNINGで受け取る列に限る。
    op.execute("GRANT INSERT ON public.pipeline_events TO vector_article_analysis")
    op.execute(
        "GRANT SELECT (id, occurred_at) ON public.pipeline_events "
        "TO vector_article_analysis"
    )
    op.execute("GRANT INSERT ON public.outbox_events TO vector_article_analysis")
    op.execute(
        "GRANT SELECT (event_id, schema_version, occurred_at, next_attempt_at, "
        "attempt_count) ON public.outbox_events TO vector_article_analysis"
    )
    op.execute(
        _sequence_grants("GRANT USAGE ON SEQUENCE %s TO vector_article_analysis")
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(
        _sequence_grants("REVOKE USAGE ON SEQUENCE %s FROM vector_article_analysis")
    )
    op.execute(
        "REVOKE SELECT (event_id, schema_version, occurred_at, next_attempt_at, "
        "attempt_count) ON public.outbox_events FROM vector_article_analysis"
    )
    op.execute("REVOKE INSERT ON public.outbox_events FROM vector_article_analysis")
    op.execute(
        "REVOKE SELECT (id, occurred_at) ON public.pipeline_events "
        "FROM vector_article_analysis"
    )
    op.execute("REVOKE INSERT ON public.pipeline_events FROM vector_article_analysis")
    op.execute(
        "REVOKE UPDATE (embedding) ON public.analyzed_articles "
        "FROM vector_article_analysis"
    )
    op.execute(
        "REVOKE SELECT, INSERT ON public.article_curations, public.curation_noises, "
        "public.analyzed_articles, public.out_of_scope_articles "
        "FROM vector_article_analysis"
    )
    op.execute(
        "REVOKE SELECT ON public.analyzable_articles, public.categories "
        "FROM vector_article_analysis"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_article_analysis")
