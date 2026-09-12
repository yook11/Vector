"""検証対象イメージ内で、正本によるDB準備と実状態の確認を行う。"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager, closing
from pathlib import Path

import asyncpg
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from botocore.config import Config
from botocore.session import Session

from alembic import command
from app.db.engine import create_migration_engine
from app.db.iam import build_iam_password_provider
from app.db.migration.settings import MigrationSettings
from app.db.ssl import split_ssl_from_url

# migration専用キーとは分け、別接続で行うmigrationを妨げない。
_PREPARE_LOCK_KEY = 0x56454350524550
_AUTH_TABLES = (
    'auth."user"',
    "auth.account",
    "auth.session",
    "auth.verification",
    'auth."rateLimit"',
)


class PreparationError(RuntimeError):
    """秘密値を含まない固定の理由コードを準備結果へ返す。"""


@asynccontextmanager
async def preparation_lock(connection):
    if not await connection.fetchval(
        "SELECT pg_try_advisory_lock($1)", _PREPARE_LOCK_KEY
    ):
        raise PreparationError("database_prepare_in_progress")
    try:
        yield
    finally:
        if connection.is_in_transaction():
            await connection.execute("ROLLBACK")
        await connection.fetchval("SELECT pg_advisory_unlock($1)", _PREPARE_LOCK_KEY)


async def database_state(connection):
    if await connection.fetchval("SELECT to_regclass('public.alembic_version')"):
        return "existing"
    partial = await connection.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'auth')
          OR EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND NOT EXISTS (
                SELECT 1 FROM pg_depend d
                WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                  AND d.deptype = 'e'
            )
          )
        """
    )
    if partial:
        raise PreparationError("database_incomplete_destroy_then_use_new_run_id")
    return "fresh"


async def verify_database(owner, application, expected_heads):
    try:
        heads = {
            row["version_num"]
            for row in await owner.fetch(
                "SELECT version_num FROM public.alembic_version"
            )
        }
        if not expected_heads or heads != set(expected_heads):
            raise PreparationError(
                "database_revision_mismatch_destroy_then_use_new_run_id"
            )
        extension = await owner.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
        )
        tables = await owner.fetchval(
            "SELECT bool_and(to_regclass(name) IS NOT NULL) "
            "FROM unnest($1::text[]) AS name",
            list(_AUTH_TABLES),
        )
        seeds = await application.fetchval(
            "SELECT EXISTS (SELECT 1 FROM public.categories) "
            "AND EXISTS (SELECT 1 FROM public.news_sources)"
        )
        await application.fetch(
            "SELECT id, embedding FROM public.analyzed_articles LIMIT 0"
        )
        if not (extension and tables and seeds):
            raise PreparationError(
                "database_required_state_missing_destroy_then_use_new_run_id"
            )
    except (
        asyncpg.UndefinedTableError,
        asyncpg.UndefinedColumnError,
        asyncpg.InvalidSchemaNameError,
        asyncpg.InsufficientPrivilegeError,
    ) as error:
        raise PreparationError(
            "database_required_state_missing_destroy_then_use_new_run_id"
        ) from error
    return sorted(heads)


async def provision_roles(master):
    sql = Path("/prepare/db-provision.sql").read_text()
    # 正本で使うpsqlのgexecだけを実行し、未知のメタコマンドは受け入れない。
    sql = "\n".join(
        line
        for line in sql.splitlines()
        if not line.lstrip().startswith("--") and line != "\\set ON_ERROR_STOP on"
    )
    chunks = sql.split("\\gexec")
    for chunk in chunks[:-1]:
        prefix, separator, query = chunk.rpartition(";")
        if "\\" in chunk:
            raise PreparationError("unsupported_provision_command")
        if separator:
            await master.execute(prefix + ";")
        for row in await master.fetch(query):
            await master.execute(row[0])
    if "\\" in chunks[-1]:
        raise PreparationError("unsupported_provision_command")
    await master.execute(chunks[-1])


async def prepare_auth(owner):
    # pg_dumpの実行制限マーカー以外は加工せず、正本CLIが生成したDDLを適用する。
    sql = "\n".join(
        line
        for line in Path("/prepare/auth.sql").read_text().splitlines()
        if not line.startswith(("\\restrict ", "\\unrestrict "))
    )
    if any(line.startswith("\\") for line in sql.splitlines()):
        raise PreparationError("unsupported_auth_dump_command")
    async with owner.transaction():
        await owner.execute(sql)


async def migrate(url, config):
    settings = MigrationSettings(
        env="production",
        migration_database_url=url,
        db_iam_auth=True,
        aws_region="ap-northeast-1",
    )
    engine = create_migration_engine(settings)
    try:
        async with engine.connect() as connection:

            def upgrade(sync_connection):
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(upgrade)
    finally:
        await engine.dispose()


async def prepare_locked(master, connect_role, migrate_database, expected_heads):
    async with preparation_lock(master):
        state = await database_state(master)
        if state == "fresh":
            await provision_roles(master)
            async with connect_role("vector") as owner:
                await prepare_auth(owner)
            await migrate_database()
        async with (
            connect_role("vector") as owner,
            connect_role("vector_app") as application,
        ):
            heads = await verify_database(owner, application, expected_heads)
        return {
            "status": "prepared",
            "heads": heads,
            "verified": True,
            "action": "initialized" if state == "fresh" else "verified",
        }


async def prepare(settings):
    host = settings["address"]
    url = f"postgresql+asyncpg://vector@{host}:5432/vector?sslmode=require"
    _, tls = split_ssl_from_url(url)
    sdk = Session()
    with closing(
        sdk.create_client(
            "secretsmanager",
            region_name="ap-northeast-1",
            config=Config(
                connect_timeout=5, read_timeout=10, ignore_configured_endpoint_urls=True
            ),
        )
    ) as client:
        secret = json.loads(
            client.get_secret_value(SecretId=settings["master_secret_arn"])[
                "SecretString"
            ]
        )
    if secret["username"] != "vector_master":
        raise PreparationError("unexpected_master_user")
    master = await asyncpg.connect(
        host=host,
        database="vector",
        user="vector_master",
        password=secret["password"],
        **tls,
        timeout=10,
        command_timeout=60,
    )
    del secret
    try:
        with closing(
            sdk.create_client(
                "rds",
                region_name="ap-northeast-1",
                config=Config(proxies={}, ignore_configured_endpoint_urls=True),
            )
        ) as rds:

            @asynccontextmanager
            async def connect_role(role):
                role_url = (
                    f"postgresql+asyncpg://{role}@{host}:5432/vector?sslmode=require"
                )
                provider = build_iam_password_provider(
                    role_url,
                    region="ap-northeast-1",
                    generate_token=rds.generate_db_auth_token,
                )
                try:
                    connection = await asyncpg.connect(
                        host=host,
                        database="vector",
                        user=role,
                        password=await provider(),
                        **tls,
                        timeout=10,
                        command_timeout=60,
                    )
                except Exception as error:
                    raise PreparationError("database_connection_failed") from error
                try:
                    yield connection
                finally:
                    await connection.close(timeout=5)

            config = AlembicConfig("/app/alembic.ini")
            result = await prepare_locked(
                master,
                connect_role,
                lambda: migrate(url, config),
                ScriptDirectory.from_config(config).get_heads(),
            )
    finally:
        await master.close(timeout=5)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(prepare(json.loads(sys.argv[1])), timeout=600))
    except Exception as error:
        reason = (
            str(error)
            if isinstance(error, PreparationError)
            else "database_prepare_timeout"
            if isinstance(error, TimeoutError)
            else "database_operation_failed"
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": reason,
                    "error_type": type(error).__name__,
                }
            )
        )
        raise SystemExit(1) from None
