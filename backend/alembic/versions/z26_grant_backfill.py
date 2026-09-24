"""救済（backfill）ロールに対象抽出・期限切れ整理・監査の追加を許可する。

Revision ID: z26_grant_backfill
Revises: z25_grant_article_analysis
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "z26_grant_backfill"
down_revision: str | None = "z25_grant_article_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"

ROLE_NAME = "vector_backfill"


def _sequence_grant(statement: str) -> str:
    return f"""
        DO $$
        DECLARE
          seq text := pg_get_serial_sequence('public.pipeline_events', 'id');
        BEGIN
          IF seq IS NULL THEN
            RAISE EXCEPTION 'id sequence of pipeline_events is missing';
          END IF;
          EXECUTE format('{statement}', seq);
        END $$;
    """


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    if not op.get_bind().scalar(
        text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :role)"),
        {"role": ROLE_NAME},
    ):
        raise RuntimeError("Create vector_backfill before migration")

    # 接続先DBへ付与し、本番と隔離したテストDBで同じmigrationを使用する。
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO vector_backfill',
                current_database()
            );
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO vector_backfill")
    op.execute(
        "GRANT SELECT ON public.analyzable_articles, public.article_curations, "
        "public.curation_noises, public.analyzed_articles, "
        "public.out_of_scope_articles, public.assessment_backfill_exclusions, "
        "public.embedding_backfill_exclusions, public.incomplete_articles, "
        "public.news_sources TO vector_backfill"
    )
    op.execute("GRANT DELETE ON public.analyzable_articles TO vector_backfill")
    # 更新しない3表は、整理前の行ロック（FOR UPDATE）に必要なid列の更新だけを許す。
    op.execute(
        "GRANT UPDATE (id) ON public.analyzable_articles, public.article_curations, "
        "public.analyzed_articles TO vector_backfill"
    )
    op.execute(
        "GRANT INSERT ON public.assessment_backfill_exclusions, "
        "public.embedding_backfill_exclusions TO vector_backfill"
    )
    op.execute(
        "GRANT UPDATE (status, leased_until, updated_at) "
        "ON public.incomplete_articles TO vector_backfill"
    )
    # 監査は追加だけとし、SELECTはORMがRETURNINGで受け取る列に限る。
    op.execute("GRANT INSERT ON public.pipeline_events TO vector_backfill")
    op.execute(
        "GRANT SELECT (id, occurred_at) ON public.pipeline_events TO vector_backfill"
    )
    op.execute(_sequence_grant("GRANT USAGE ON SEQUENCE %s TO vector_backfill"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(_sequence_grant("REVOKE USAGE ON SEQUENCE %s FROM vector_backfill"))
    op.execute(
        "REVOKE SELECT (id, occurred_at) ON public.pipeline_events FROM vector_backfill"
    )
    op.execute("REVOKE INSERT ON public.pipeline_events FROM vector_backfill")
    op.execute(
        "REVOKE UPDATE (status, leased_until, updated_at) "
        "ON public.incomplete_articles FROM vector_backfill"
    )
    op.execute(
        "REVOKE INSERT ON public.assessment_backfill_exclusions, "
        "public.embedding_backfill_exclusions FROM vector_backfill"
    )
    op.execute(
        "REVOKE UPDATE (id) ON public.analyzable_articles, public.article_curations, "
        "public.analyzed_articles FROM vector_backfill"
    )
    op.execute("REVOKE DELETE ON public.analyzable_articles FROM vector_backfill")
    op.execute(
        "REVOKE SELECT ON public.analyzable_articles, public.article_curations, "
        "public.curation_noises, public.analyzed_articles, "
        "public.out_of_scope_articles, public.assessment_backfill_exclusions, "
        "public.embedding_backfill_exclusions, public.incomplete_articles, "
        "public.news_sources FROM vector_backfill"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_backfill")
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'REVOKE CONNECT ON DATABASE %I FROM vector_backfill',
                current_database()
            );
        END $$
    """)
