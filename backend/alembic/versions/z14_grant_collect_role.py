"""grant_collect_role

collect worker 専用の最小権限 role vector_collect に、acquisition (stage1) +
completion (stage2) が実際に触れる public schema の 4 table だけへ DML 権限を
付与する。本番 backend を vector-core / vector-collect の 2 app に分割した結果、
collect (untrusted HTML を fetch/parse し RCE/SSRF 攻撃面を持つ) を vector_app
(public.* 全 table へ DML) のまま走らせると侵害時の blast radius が過大になる
ため、触る面だけに絞る。

権限マトリクス (workflow trace で網羅確認):
- incomplete_articles: SELECT, INSERT, UPDATE, DELETE  (acq が open で INSERT、
                       completion が lease 状態機械で全 DML)
- articles:            SELECT, INSERT                   (両 stage が INSERT、
                       dedup / 監査逆引きで SELECT。UPDATE/DELETE は持たせない)
- news_sources:        SELECT                           (dispatch が active
                       source を読むのみ)
- pipeline_events:     INSERT + SELECT (id, occurred_at) (監査 append-only INSERT。
                       ORM session.add の RETURNING id, occurred_at に必要な列
                       SELECT だけ。payload は読めないため append-only 性を維持)

設計判断:
- REFERENCES なし: FK 検証は RI トリガが「親 table 所有者権限」で走るため、
  child に INSERT する role に親表の REFERENCES/SELECT は不要
  (https://www.postgresql.org/docs/18/ddl-priv.html)。
- default privileges なし: 今後 vector が CREATE する新 table へは自動付与せず、
  必要になった時点で明示 grant を migration で足す (最小権限の代償として明示性を取る)。
- INSERT する 3 table の id sequence にのみ USAGE を付与 (news_sources は read-only)。
  sequence 名は rename 履歴に強い pg_get_serial_sequence で解決する。

role 自体の作成は infra/db/init/01_create_app_users.sh / CI の role 作成 step が
担う (n3_grant_app_db_users と同方針)。本 migration は role 不在なら友好的
エラーで停止する。

Revision ID: z14_grant_collect_role
Revises: z13_trend_discovery_audit_stage
Create Date: 2026-05-31 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "z14_grant_collect_role"
down_revision: str | None = "z13_trend_discovery_audit_stage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # role 不在ならガイダンス付きで停止する。新規 dev は init script で自動作成、
    # 既存 dev は手動 CREATE ROLE (PR 本文に記載) を踏むこと。
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vector_collect') THEN
            RAISE EXCEPTION
              'role vector_collect not found. '
              'Run infra/db/init/01_create_app_users.sh '
              'or create the role manually before running this migration.';
          END IF;
        END $$;
        """
    )

    # public schema への到達権 (table-level 権限の前提)。
    op.execute("GRANT USAGE ON SCHEMA public TO vector_collect")

    # table 単位の最小 DML (ALL TABLES は使わない)。
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON incomplete_articles TO vector_collect"
    )
    op.execute("GRANT SELECT, INSERT ON articles TO vector_collect")
    op.execute("GRANT SELECT ON news_sources TO vector_collect")
    # 監査は append-only INSERT。SELECT は RETURNING に出る id, occurred_at の
    # 2 列だけに絞り、payload (本文・外部入力) は読めないようにする。
    op.execute("GRANT INSERT ON pipeline_events TO vector_collect")
    op.execute("GRANT SELECT (id, occurred_at) ON pipeline_events TO vector_collect")

    # INSERT する table の id sequence にのみ USAGE。sequence 名は
    # pg_get_serial_sequence で解決し、SERIAL/IDENTITY や rename 履歴に依存しない。
    op.execute(
        """
        DO $$
        DECLARE
          t text;
          seq text;
        BEGIN
          FOREACH t IN ARRAY ARRAY['incomplete_articles', 'articles', 'pipeline_events']
          LOOP
            seq := pg_get_serial_sequence('public.' || t, 'id');
            IF seq IS NOT NULL THEN
              EXECUTE format('GRANT USAGE ON SEQUENCE %s TO vector_collect', seq);
            END IF;
          END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    # GRANT を対称に REVOKE する。role 自体は残す (init script 経由のため
    # downgrade で削除しない方針: 手動 DROP ROLE が必要なときは別途実行)。
    op.execute(
        """
        DO $$
        DECLARE
          t text;
          seq text;
        BEGIN
          FOREACH t IN ARRAY ARRAY['incomplete_articles', 'articles', 'pipeline_events']
          LOOP
            seq := pg_get_serial_sequence('public.' || t, 'id');
            IF seq IS NOT NULL THEN
              EXECUTE format('REVOKE USAGE ON SEQUENCE %s FROM vector_collect', seq);
            END IF;
          END LOOP;
        END $$;
        """
    )
    op.execute("REVOKE SELECT (id, occurred_at) ON pipeline_events FROM vector_collect")
    op.execute("REVOKE INSERT ON pipeline_events FROM vector_collect")
    op.execute("REVOKE SELECT ON news_sources FROM vector_collect")
    op.execute("REVOKE SELECT, INSERT ON articles FROM vector_collect")
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE "
        "ON incomplete_articles FROM vector_collect"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM vector_collect")
