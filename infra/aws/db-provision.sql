-- RDS «vector» の初期構築 (role / 所有権 / extension)。冪等で、秘密値を含まない。
-- 実行者: vector_master (踏み台の port forward 越し)。手順は private runbook 側、
-- 制約は infra/aws/README.md。dev / test の同等物は infra/db/init/01_create_app_users.sh。
--
-- app role が password を持たないのが dev / test との差分 (認証は IAM の auth token。
-- 入った後に何ができるかは migration の GRANT が全部決める)。

-- migration 用 owner role。password 認証を続ける (app/db_iam_auth.py の射程の注記と対)。
-- password は本 script に置かない。実行後に \password vector で対話設定する。
SELECT 'CREATE ROLE vector WITH LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vector')
\gexec

SELECT format('CREATE ROLE %I WITH LOGIN', r)
FROM unnest(ARRAY['vector_auth', 'vector_app', 'vector_collect']) AS r
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
\gexec

GRANT rds_iam TO vector_auth, vector_app, vector_collect;

-- 所有権を owner へ移す。RDS の master は superuser ではないため、
-- ALTER ... OWNER TO には対象 role の membership が要る。
GRANT vector TO vector_master;
ALTER DATABASE vector OWNER TO vector;
ALTER SCHEMA public OWNER TO vector;

-- migration にも CREATE EXTENSION IF NOT EXISTS vector があるが、owner 権限で
-- 作れない環境に備えて master 側でも先に作る (冪等なので二重でも無害)。
CREATE EXTENSION IF NOT EXISTS vector;
