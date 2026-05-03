"""grant_app_db_users

Postgres user 分離 (red-team AUTH-N4): application 接続専用の vector_auth /
vector_app role に schema-scoped DML 権限を付与し、反対側 schema からは
REVOKE で構造的に遮断する。

vector_auth: auth.* に SELECT/INSERT/UPDATE/DELETE。public.* は触れない。
vector_app:  public.* に SELECT/INSERT/UPDATE/DELETE + auth.user に
             REFERENCES/SELECT (watchlist_entries.user_id FK 用)。

ownership は vector のまま (alembic migration runner として継続利用)。
ALTER DEFAULT PRIVILEGES FOR ROLE vector で、今後 vector が CREATE する
新規 table へも自動で application role に DML 権限が付与される。

Role 自体の作成は infra/db/init/01_create_app_users.sh が担う (Postgres
docker-entrypoint-initdb.d 機構)。本 migration は role 不在なら友好的
エラーで停止する。

Revision ID: n3_grant_app_db_users
Revises: n2_window_end_rename
Create Date: 2026-05-03 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "n3_grant_app_db_users"
down_revision: str | None = "n2_window_end_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # role 不在ならガイダンス付きで停止する。新規 dev は init script で自動作成、
    # 既存 dev は手動で CREATE ROLE する手順 (PR 本文に記載) を踏むこと。
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vector_auth') THEN
            RAISE EXCEPTION
              'role vector_auth not found. '
              'Run infra/db/init/01_create_app_users.sh '
              'or create the role manually before running this migration.';
          END IF;
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vector_app') THEN
            RAISE EXCEPTION
              'role vector_app not found. '
              'Run infra/db/init/01_create_app_users.sh '
              'or create the role manually before running this migration.';
          END IF;
        END $$;
        """
    )

    # vector_auth: auth schema 内の DML
    op.execute("GRANT USAGE ON SCHEMA auth TO vector_auth")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA auth TO vector_auth"
    )
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA auth TO vector_auth")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA auth "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vector_auth"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA auth "
        "GRANT USAGE ON SEQUENCES TO vector_auth"
    )

    # vector_app: public schema 内の DML
    op.execute("GRANT USAGE ON SCHEMA public TO vector_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA public TO vector_app"
    )
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO vector_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vector_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA public "
        "GRANT USAGE ON SEQUENCES TO vector_app"
    )

    # cross-schema FK: watchlist_entries.user_id REFERENCES auth.user.id
    # vector_app は auth.user に対して参照整合性チェックが必要 (REFERENCES)
    # と FK 値の存在確認が必要 (SELECT)。INSERT/UPDATE/DELETE は付与しない。
    # schema USAGE がないと table-level 権限に到達できないため両方 GRANT する。
    # auth.account / auth.session / auth.verification には GRANT しないため、
    # vector_app からは auth.user 以外へのアクセスは権限拒否される (table 単位で隔離)。
    op.execute("GRANT USAGE ON SCHEMA auth TO vector_app")
    op.execute("GRANT REFERENCES, SELECT ON auth.user TO vector_app")

    # vector_auth は public schema に対する USAGE すら持たないので明示 REVOKE。
    # vector_app は auth schema USAGE を必要とする (上で GRANT 済) ため REVOKE ALL
    # は使わず、各テーブルへの個別権限不在で隔離を実現する。
    op.execute("REVOKE ALL ON SCHEMA public FROM vector_auth")


def downgrade() -> None:
    # GRANT/REVOKE を完全反転する。role 自体は残す (init script 経由のため
    # downgrade で削除しない方針: 手動 DROP ROLE が必要なときは別途実行)。
    op.execute("GRANT ALL ON SCHEMA public TO vector_auth")
    op.execute("REVOKE REFERENCES, SELECT ON auth.user FROM vector_app")
    op.execute("REVOKE USAGE ON SCHEMA auth FROM vector_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA public "
        "REVOKE USAGE ON SEQUENCES FROM vector_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM vector_app"
    )
    op.execute("REVOKE USAGE ON ALL SEQUENCES IN SCHEMA public FROM vector_app")
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA public FROM vector_app"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA auth "
        "REVOKE USAGE ON SEQUENCES FROM vector_auth"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE vector IN SCHEMA auth "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM vector_auth"
    )
    op.execute("REVOKE USAGE ON ALL SEQUENCES IN SCHEMA auth FROM vector_auth")
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA auth FROM vector_auth"
    )
    op.execute("REVOKE USAGE ON SCHEMA auth FROM vector_auth")
