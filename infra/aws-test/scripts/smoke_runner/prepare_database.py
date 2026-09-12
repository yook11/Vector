"""検証対象backendイメージ内で、既存正本から空の試験RDSを準備する。"""

import asyncio
import json
import sys
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from app.db.engine import create_migration_engine
from app.db.iam import build_iam_password_provider
from app.db.migration.settings import MigrationSettings
from app.db.ssl import split_ssl_from_url
from botocore.config import Config
from botocore.session import Session


async def prepare(settings):
    host = settings["address"]
    url = f"postgresql+asyncpg://vector@{host}:5432/vector?sslmode=require"
    _, tls = split_ssl_from_url(url)
    sdk = Session()
    with sdk.create_client(
        "secretsmanager",
        region_name="ap-northeast-1",
        config=Config(
            connect_timeout=5, read_timeout=10, ignore_configured_endpoint_urls=True
        ),
    ) as client:
        secret = json.loads(
            client.get_secret_value(SecretId=settings["master_secret_arn"])[
                "SecretString"
            ]
        )
    if secret["username"] != "vector_master":
        raise RuntimeError("unexpected_master_user")
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
        if await master.fetchval("SELECT to_regclass('public.alembic_version')"):
            raise RuntimeError("database_is_not_fresh")
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
                raise RuntimeError("unsupported_provision_command")
            if separator:
                await master.execute(prefix + ";")
            for row in await master.fetch(query):
                await master.execute(row[0])
        if "\\" in chunks[-1]:
            raise RuntimeError("unsupported_provision_command")
        await master.execute(chunks[-1])
    finally:
        await master.close(timeout=5)
    with sdk.create_client(
        "rds",
        region_name="ap-northeast-1",
        config=Config(proxies={}, ignore_configured_endpoint_urls=True),
    ) as rds:
        provider = build_iam_password_provider(
            url, region="ap-northeast-1", generate_token=rds.generate_db_auth_token
        )
        owner = await asyncpg.connect(
            host=host,
            database="vector",
            user="vector",
            password=await provider(),
            **tls,
            timeout=10,
            command_timeout=60,
        )
        try:
            # pg_dumpの実行制限マーカー以外は加工せず、正本CLIが生成したDDLを適用する。
            sql = "\n".join(
                line
                for line in Path("/prepare/auth.sql").read_text().splitlines()
                if not line.startswith(("\\restrict ", "\\unrestrict "))
            )
            if any(line.startswith("\\") for line in sql.splitlines()):
                raise RuntimeError("unsupported_auth_dump_command")
            async with owner.transaction():
                await owner.execute(sql)
        finally:
            await owner.close(timeout=5)
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
                config = AlembicConfig("/app/alembic.ini")
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")
                return ScriptDirectory.from_config(config).get_heads()

            heads = await connection.run_sync(upgrade)
    finally:
        await engine.dispose()
    print(json.dumps({"status": "prepared", "heads": heads}))


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(prepare(json.loads(sys.argv[1])), timeout=600))
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
