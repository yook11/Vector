"""agentロールに回答生成で使う参照と、runの実行・回答の保存に要る書き込みを許可する。

Revision ID: z29_grant_agent
Revises: z28_grant_insights
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "z29_grant_agent"
down_revision: str | None = "z28_grant_insights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"

ROLE_NAME = "vector_agent"
_SEQUENCE_TABLES = ("agent_message_sources", "query_embedding_cache")


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
        raise RuntimeError("Create vector_agent before migration")

    # 接続先DBへ付与し、本番と隔離したテストDBで同じmigrationを使用する。
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO vector_agent',
                current_database()
            );
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO vector_agent")
    op.execute(
        "GRANT SELECT ON public.analyzable_articles, public.article_curations, "
        "public.analyzed_articles, public.categories, public.news_sources, "
        "public.weekly_briefings, public.trends_snapshots TO vector_agent"
    )
    op.execute("GRANT SELECT, UPDATE ON public.agent_runs TO vector_agent")
    # user_idを書き換えるとスレッドが他の利用者に移るため、更新は2列に限る。
    op.execute("GRANT SELECT ON public.agent_threads TO vector_agent")
    op.execute(
        "GRANT UPDATE (updated_at, research_handoff) ON public.agent_threads "
        "TO vector_agent"
    )
    op.execute(
        "GRANT SELECT, INSERT ON public.agent_messages, "
        "public.agent_message_sources, public.query_embedding_cache TO vector_agent"
    )
    op.execute("GRANT SELECT ON public.agent_user_daily_quotas TO vector_agent")
    op.execute(
        "GRANT UPDATE (used_count) ON public.agent_user_daily_quotas TO vector_agent"
    )
    op.execute(_sequence_grants("GRANT USAGE ON SEQUENCE %s TO vector_agent"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(_sequence_grants("REVOKE USAGE ON SEQUENCE %s FROM vector_agent"))
    op.execute(
        "REVOKE UPDATE (used_count) ON public.agent_user_daily_quotas FROM vector_agent"
    )
    op.execute("REVOKE SELECT ON public.agent_user_daily_quotas FROM vector_agent")
    op.execute(
        "REVOKE SELECT, INSERT ON public.agent_messages, "
        "public.agent_message_sources, public.query_embedding_cache FROM vector_agent"
    )
    op.execute(
        "REVOKE UPDATE (updated_at, research_handoff) ON public.agent_threads "
        "FROM vector_agent"
    )
    op.execute("REVOKE SELECT ON public.agent_threads FROM vector_agent")
    op.execute("REVOKE SELECT, UPDATE ON public.agent_runs FROM vector_agent")
    op.execute(
        "REVOKE SELECT ON public.analyzable_articles, public.article_curations, "
        "public.analyzed_articles, public.categories, public.news_sources, "
        "public.weekly_briefings, public.trends_snapshots FROM vector_agent"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_agent")
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'REVOKE CONNECT ON DATABASE %I FROM vector_agent',
                current_database()
            );
        END $$
    """)
