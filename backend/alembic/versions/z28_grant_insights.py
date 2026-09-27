"""Insightsロールに分析結果の参照と、トレンド・ブリーフィング・監査の追加を許可する。

Revision ID: z28_grant_insights
Revises: z27_grant_api
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "z28_grant_insights"
down_revision: str | None = "z27_grant_api"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"

ROLE_NAME = "vector_insights"
_SEQUENCE_TABLES = ("weekly_briefings", "pipeline_events")


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
        raise RuntimeError("Create vector_insights before migration")

    # 接続先DBへ付与し、本番と隔離したテストDBで同じmigrationを使用する。
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO vector_insights',
                current_database()
            );
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO vector_insights")
    op.execute(
        "GRANT SELECT ON public.analyzed_articles, public.categories TO vector_insights"
    )
    # 成果物は追加だけとし、既存行の上書きと削除は許さない。
    op.execute(
        "GRANT SELECT, INSERT ON public.trends_snapshots, public.weekly_briefings "
        "TO vector_insights"
    )
    # 監査は追加だけとし、SELECTはORMがRETURNINGで受け取る列に限る。
    op.execute("GRANT INSERT ON public.pipeline_events TO vector_insights")
    op.execute(
        "GRANT SELECT (id, occurred_at) ON public.pipeline_events TO vector_insights"
    )
    op.execute(_sequence_grants("GRANT USAGE ON SEQUENCE %s TO vector_insights"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(_sequence_grants("REVOKE USAGE ON SEQUENCE %s FROM vector_insights"))
    op.execute(
        "REVOKE SELECT (id, occurred_at) ON public.pipeline_events FROM vector_insights"
    )
    op.execute("REVOKE INSERT ON public.pipeline_events FROM vector_insights")
    op.execute(
        "REVOKE SELECT, INSERT ON public.trends_snapshots, public.weekly_briefings "
        "FROM vector_insights"
    )
    op.execute(
        "REVOKE SELECT ON public.analyzed_articles, public.categories "
        "FROM vector_insights"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_insights")
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'REVOKE CONNECT ON DATABASE %I FROM vector_insights',
                current_database()
            );
        END $$
    """)
