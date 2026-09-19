"""認証カウンター掃除専用ロールへ期限列の参照と削除を許可する。"""

from sqlalchemy import text

from alembic import op

revision = "z24_auth_cleanup_grants"
down_revision = "z23_grant_outbox_relay"
branch_labels = None
depends_on = None
MIGRATION_KIND = "contract"

ROLE_NAME = "vector_auth_rate_limit_cleanup"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    connection = op.get_bind()
    if not connection.scalar(
        text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :role)"),
        {"role": ROLE_NAME},
    ):
        raise RuntimeError("Create vector_auth_rate_limit_cleanup before migration")
    if not connection.scalar(text("SELECT to_regclass('auth.\"rateLimit\"')")):
        raise RuntimeError('Create auth."rateLimit" with Better Auth before migration')
    if not connection.scalar(
        text(
            "SELECT EXISTS (SELECT FROM pg_attribute "
            "WHERE attrelid = 'auth.\"rateLimit\"'::regclass "
            "AND attname = 'lastRequest' AND attnum > 0 AND NOT attisdropped)"
        )
    ):
        raise RuntimeError('auth."rateLimit" requires "lastRequest" before migration')
    # 接続先DBへ付与し、本番と隔離したテストDBで同じmigrationを使用する。
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO vector_auth_rate_limit_cleanup',
                current_database()
            );
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA auth TO vector_auth_rate_limit_cleanup")
    op.execute(
        'GRANT SELECT ("lastRequest") ON auth."rateLimit" '
        "TO vector_auth_rate_limit_cleanup"
    )
    op.execute('GRANT DELETE ON auth."rateLimit" TO vector_auth_rate_limit_cleanup')


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute('REVOKE DELETE ON auth."rateLimit" FROM vector_auth_rate_limit_cleanup')
    op.execute(
        'REVOKE SELECT ("lastRequest") ON auth."rateLimit" '
        "FROM vector_auth_rate_limit_cleanup"
    )
    op.execute("REVOKE USAGE ON SCHEMA auth FROM vector_auth_rate_limit_cleanup")
    op.execute("""
        DO $$ BEGIN
            EXECUTE format(
                'REVOKE CONNECT ON DATABASE %I FROM vector_auth_rate_limit_cleanup',
                current_database()
            );
        END $$
    """)
