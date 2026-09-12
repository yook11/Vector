"""専用Postgresを構築し、migration適用済みDBを各ケースへ貸し出す。"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

import asyncpg
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[2]
Role = Literal["vector", "vector_app", "vector_auth", "vector_collect"]


class RunnerSettings(BaseSettings):
    """外部コマンドには実行ファイルの探索パスだけを引き継ぐ。"""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    process_path: str = Field(validation_alias="PATH")


@dataclass(frozen=True)
class SystemDatabase:
    port: int
    passwords: dict[str, str] = field(repr=False)
    name: str = "vector"

    def url(self, role: Role, *, sqlalchemy: bool = False) -> str:
        return URL.create(
            "postgresql+asyncpg" if sqlalchemy else "postgresql",
            username=role,
            password=self.passwords[role],
            host="127.0.0.1",
            port=self.port,
            database=self.name,
        ).render_as_string(hide_password=False)

    @asynccontextmanager
    async def connect(self, role: Role) -> AsyncIterator[asyncpg.Connection]:
        connection = await asyncpg.connect(self.url(role), command_timeout=10)
        try:
            yield connection
        finally:
            await connection.close()


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(  # noqa: S603
        args, cwd=cwd, env=env, text=True, capture_output=True, timeout=300
    )
    if result.returncode:
        raise RuntimeError(
            f"system DB setup command failed: {args[0]}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def migration_heads() -> set[str]:
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


async def _prepare_auth(database: SystemDatabase) -> None:
    async with database.connect("vector") as connection:
        await connection.execute("CREATE SCHEMA auth")


def _migrate_auth(database: SystemDatabase, env: dict[str, str]) -> None:
    # CLIによるdotenv読込を避け、正本の設定ファイルだけを一時ディレクトリへ渡す。
    with TemporaryDirectory(prefix="vector-system-auth-") as directory:
        workspace = Path(directory)
        frontend = ROOT / "frontend"
        for relative in (
            "src/lib/auth/auth.cli.ts",
            "src/lib/auth/auth-config.ts",
            "src/lib/auth/pool-ssl.ts",
            "src/lib/env.ts",
            "package.json",
        ):
            destination = workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(frontend / relative, destination)
        (workspace / "node_modules").symlink_to(frontend / "node_modules")
        (workspace / "tsconfig.json").write_text(
            json.dumps({"compilerOptions": {"paths": {"@/*": ["./src/*"]}}})
        )
        # 既存developmentイメージと同じCLI版を使う。
        dockerfile = (frontend / "Dockerfile").read_text()
        version = next(
            line.removeprefix("ARG BETTER_AUTH_CLI_VERSION=")
            for line in dockerfile.splitlines()
            if line.startswith("ARG BETTER_AUTH_CLI_VERSION=")
        )
        _run(
            [
                "npx",
                "--yes",
                f"@better-auth/cli@{version}",
                "migrate",
                "--config",
                "src/lib/auth/auth.cli.ts",
                "--yes",
            ],
            cwd=workspace,
            env={
                **env,
                "AUTH_DATABASE_URL": database.url("vector"),
                "BETTER_AUTH_URL": "http://localhost:3000",
                "BETTER_AUTH_SECRET": "system-test-only-auth-secret-xxxxxxxxxxxxxxxx",
            },
        )


async def _verify_template(database: SystemDatabase) -> None:
    async with database.connect("vector") as connection:
        actual = {
            row["version_num"]
            for row in await connection.fetch("SELECT version_num FROM alembic_version")
        }
        if actual != migration_heads():
            raise RuntimeError("system DB migration heads do not match source")
        # CREATE DATABASE TEMPLATEはDB単位のACLを複製しないため黙って権限を変えない。
        acl = await connection.fetchval(
            "SELECT datacl FROM pg_database WHERE datname = current_database()"
        )
        if acl is not None:
            raise RuntimeError("system DB template has unsupported database-level ACL")
    async with database.connect("vector_app") as connection:
        if not await connection.fetchval("SELECT count(*) FROM categories"):
            raise RuntimeError("system DB category seed is missing")
    async with database.connect("vector_auth") as connection:
        for table in (
            'auth."user"',
            "auth.account",
            "auth.session",
            "auth.verification",
            'auth."rateLimit"',
        ):
            if not await connection.fetchval("SELECT to_regclass($1)", table):
                raise RuntimeError(f"system DB auth table is missing: {table}")
    async with replace(database, name="postgres").connect("vector") as connection:
        await connection.execute("ALTER DATABASE vector ALLOW_CONNECTIONS false")


@contextmanager
def migrated_database() -> Iterator[SystemDatabase]:
    """実行専用環境だけを操作し、初期化の失敗時もコンテナを削除する。"""
    env = {"PATH": RunnerSettings().process_path}
    project = f"vector-test-system-{uuid4().hex[:12]}"
    compose = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-p",
        project,
        "-f",
        str(ROOT / "docker-compose.test.yml"),
    ]
    config = json.loads(
        _run([*compose, "config", "--format", "json"], cwd=ROOT, env=env)
    )
    values = config["services"]["db-test"]["environment"]
    if values["POSTGRES_USER"] != "vector" or values["POSTGRES_DB"] != "vector":
        raise RuntimeError("system DB requires the vector bootstrap role and database")
    try:
        _run([*compose, "up", "-d", "--wait", "db-test"], cwd=ROOT, env=env)
        address = _run([*compose, "port", "db-test", "5432"], cwd=ROOT, env=env).strip()
        host, port = address.rsplit(":", 1)
        if host != "127.0.0.1":
            raise RuntimeError("system DB must use a loopback port")
        database = SystemDatabase(
            int(port),
            {
                "vector": values["POSTGRES_PASSWORD"],
                "vector_app": values["POSTGRES_APP_PASSWORD"],
                "vector_auth": values["POSTGRES_AUTH_PASSWORD"],
                "vector_collect": values["POSTGRES_COLLECT_PASSWORD"],
            },
        )
        asyncio.run(_prepare_auth(database))
        _migrate_auth(database, env)
        _run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT / "backend",
            env={
                **env,
                "MIGRATION_DATABASE_URL": database.url("vector", sqlalchemy=True),
                "ALEMBIC_ALLOW_DESTRUCTIVE": "yes-i-know",
            },
        )
        asyncio.run(_verify_template(database))
        yield database
    finally:
        _run([*compose, "down", "-v", "--remove-orphans"], cwd=ROOT, env=env)


@asynccontextmanager
async def isolated_database(template: SystemDatabase) -> AsyncIterator[SystemDatabase]:
    """migration・権限・初期データを複製し、ケースのcommitを持ち越さない。"""
    admin = replace(template, name="postgres")
    async with admin.connect("vector") as connection:
        await connection.execute("CREATE DATABASE system_test TEMPLATE vector")
    try:
        yield replace(template, name="system_test")
    finally:
        async with admin.connect("vector") as connection:
            await connection.execute("DROP DATABASE system_test WITH (FORCE)")
