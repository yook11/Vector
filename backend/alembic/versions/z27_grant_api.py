"""APIロールにニュース・成果物の参照と、利用者・運用者の操作に対応する書き込みを許可する。

Revision ID: z27_grant_api
Revises: z26_grant_backfill
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "z27_grant_api"
down_revision: str | None = "z26_grant_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"

ROLE_NAME = "vector_api"


def _sequence_grant(statement: str) -> str:
    return f"""
        DO $$
        DECLARE
          seq text := pg_get_serial_sequence('public.news_sources', 'id');
        BEGIN
          IF seq IS NULL THEN
            RAISE EXCEPTION 'id sequence of news_sources is missing';
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
        raise RuntimeError("Create vector_api before migration")

    # 接続先DBへ付与し、本番と隔離したテストDBで同じmigrationを使用する。
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO vector_api',
                current_database()
            );
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO vector_api")
    op.execute(
        "GRANT SELECT ON public.analyzable_articles, public.article_curations, "
        "public.analyzed_articles, public.categories, public.weekly_briefings, "
        "public.trends_snapshots, public.incomplete_articles, "
        "public.curation_noises, public.out_of_scope_articles, "
        "public.assessment_backfill_exclusions, "
        "public.embedding_backfill_exclusions, public.agent_message_sources "
        "TO vector_api"
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON public.news_sources TO vector_api")
    op.execute(
        "GRANT UPDATE (is_active, updated_at) ON public.news_sources TO vector_api"
    )
    op.execute(_sequence_grant("GRANT USAGE ON SEQUENCE %s TO vector_api"))
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON public.watchlist_entries, "
        "public.agent_threads TO vector_api"
    )
    op.execute("GRANT UPDATE (updated_at) ON public.agent_threads TO vector_api")
    op.execute(
        "GRANT SELECT, INSERT ON public.agent_messages, public.agent_runs, "
        "public.agent_user_daily_quotas TO vector_api"
    )
    # 回答の結び付けと実行の記録はworkerが書くため、取消と期限回収で変わる列に限る。
    op.execute("GRANT UPDATE (status, error_code) ON public.agent_runs TO vector_api")
    op.execute(
        "GRANT UPDATE (used_count) ON public.agent_user_daily_quotas TO vector_api"
    )
    # 監査は健全性画面が集計する列だけを読ませ、payload等は読ませない。
    op.execute(
        "GRANT SELECT (stage, event_type, outcome_code, source_id, occurred_at) "
        "ON public.pipeline_events TO vector_api"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(
        "REVOKE SELECT (stage, event_type, outcome_code, source_id, occurred_at) "
        "ON public.pipeline_events FROM vector_api"
    )
    op.execute(
        "REVOKE UPDATE (used_count) ON public.agent_user_daily_quotas FROM vector_api"
    )
    op.execute(
        "REVOKE UPDATE (status, error_code) ON public.agent_runs FROM vector_api"
    )
    op.execute(
        "REVOKE SELECT, INSERT ON public.agent_messages, public.agent_runs, "
        "public.agent_user_daily_quotas FROM vector_api"
    )
    op.execute("REVOKE UPDATE (updated_at) ON public.agent_threads FROM vector_api")
    op.execute(
        "REVOKE SELECT, INSERT, DELETE ON public.watchlist_entries, "
        "public.agent_threads FROM vector_api"
    )
    op.execute(_sequence_grant("REVOKE USAGE ON SEQUENCE %s FROM vector_api"))
    op.execute(
        "REVOKE UPDATE (is_active, updated_at) ON public.news_sources FROM vector_api"
    )
    op.execute("REVOKE SELECT, INSERT, DELETE ON public.news_sources FROM vector_api")
    op.execute(
        "REVOKE SELECT ON public.analyzable_articles, public.article_curations, "
        "public.analyzed_articles, public.categories, public.weekly_briefings, "
        "public.trends_snapshots, public.incomplete_articles, "
        "public.curation_noises, public.out_of_scope_articles, "
        "public.assessment_backfill_exclusions, "
        "public.embedding_backfill_exclusions, public.agent_message_sources "
        "FROM vector_api"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_api")
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'REVOKE CONNECT ON DATABASE %I FROM vector_api',
                current_database()
            );
        END $$
    """)
