"""調査ロールにpublicの全表の参照を許可し、今後作る表にも参照を自動で付与する。

Revision ID: z31_grant_investigation
Revises: z30_grant_insights_publication
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "z31_grant_investigation"
down_revision: str | None = "z30_grant_insights_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"

ROLE_NAME = "vector_investigation"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    if not op.get_bind().scalar(
        text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :role)"),
        {"role": ROLE_NAME},
    ):
        raise RuntimeError("Create vector_investigation before migration")

    # 接続先DBへ付与し、本番と隔離したテストDBで同じmigrationを使用する。
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO vector_investigation',
                current_database()
            );
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO vector_investigation")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO vector_investigation")
    # 表を追加するたびにGRANTのmigrationを要さないよう、vectorが今後作る表にも付与する。
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA public "
        "GRANT SELECT ON TABLES TO vector_investigation"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA public "
        "REVOKE SELECT ON TABLES FROM vector_investigation"
    )
    op.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM vector_investigation")
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_investigation")
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'REVOKE CONNECT ON DATABASE %I FROM vector_investigation',
                current_database()
            );
        END $$
    """)
