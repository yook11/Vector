"""対象revisionの正本から、空のローカルDBで認証schemaだけを生成する。"""

import hashlib
import json
import os
import secrets
import subprocess
import time
from uuid import uuid4

from .common import ROOT, execute, save

ASSET_FILES = ("auth.sql", "source/infra/aws/db-provision.sql")


def prepare_assets(directory, revision):
    destination = directory / "prepared-assets"
    if destination.exists():
        manifest = json.loads((destination / "manifest.json").read_text())
        if manifest != {
            "revision": revision,
            "files": asset_hashes(destination),
        }:
            raise RuntimeError("prepared_assets_changed")
        return
    work = directory / "assets-work"
    if work.exists():
        work.rename(directory / ("assets-failed-" + uuid4().hex))
    work.mkdir()
    try:
        _generate_assets(work, revision, "vector-smoke-auth-" + directory.name)
        save(
            work / "manifest.json", {"revision": revision, "files": asset_hashes(work)}
        )
        work.rename(destination)
    except BaseException:
        if work.exists():
            work.rename(directory / ("assets-failed-" + uuid4().hex))
        raise


def asset_hashes(directory):
    hashes = {}
    for name in ASSET_FILES:
        content = (directory / name).read_bytes()
        if not content.strip():
            raise RuntimeError("prepared_asset_empty")
        hashes[name] = hashlib.sha256(content).hexdigest()
    return hashes


def _generate_assets(directory, revision, name):
    # 中断した同じRUN_IDの一時コンテナだけを回収してから再生成する。
    for args in (
        ["docker", "rm", "-fv", name + "-cli", name],
        ["docker", "network", "rm", name],
    ):
        subprocess.run(args, capture_output=True, timeout=30)  # noqa: S603
    source = directory / "source"
    source.mkdir()
    archive = directory / "source.tar"
    with archive.open("wb") as output:
        subprocess.run(  # noqa: S603,S607
            ["git", "archive", revision, "frontend", "infra/aws/db-provision.sql"],  # noqa: S607
            cwd=ROOT,
            stdout=output,
            check=True,
            timeout=30,
        )
    execute(
        ["tar", "-xf", str(archive), "-C", str(source)],
        cwd=ROOT,
        log=directory / "source.log",
    )
    image = name + ":local"
    env = {**os.environ, "POSTGRES_PASSWORD": secrets.token_urlsafe(32)}
    # ローカルの一時DBも公開せず、CLIとDBだけのネットワークでschemaを生成する。
    execute(
        [
            "docker",
            "build",
            "--target",
            "development",
            "-t",
            image,
            str(source / "frontend"),
        ],
        cwd=ROOT,
        log=directory / "auth-build.log",
        timeout=900,
    )
    network_created = False
    try:
        execute(
            ["docker", "network", "create", "--internal", name],
            cwd=ROOT,
            log=directory / "auth-network.log",
        )
        network_created = True
        execute(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "--network",
                name,
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                "POSTGRES_USER=vector",
                "--env",
                "POSTGRES_DB=vector",
                "postgres:17-bookworm",
            ],
            cwd=ROOT,
            env=env,
            log=directory / "auth-db.log",
        )
        # 初期化用の一時サーバを成功と判定しないよう、TCP受付を待つ。
        for attempt in range(60):
            ready = subprocess.run(  # noqa: S603,S607
                [  # noqa: S607
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "vector",
                    "-d",
                    "vector",
                ],
                capture_output=True,
                timeout=10,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise TimeoutError("local_auth_database_not_ready")
        execute(
            [
                "docker",
                "exec",
                name,
                "psql",
                "-U",
                "vector",
                "-d",
                "vector",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "CREATE SCHEMA auth",
            ],
            cwd=ROOT,
            log=directory / "auth-schema.log",
        )
        env.update(
            {
                "AUTH_DATABASE_URL": (
                    f"postgresql://vector:{env['POSTGRES_PASSWORD']}@{name}:5432/vector"
                ),
                "BETTER_AUTH_URL": "http://localhost:3000",
                "BETTER_AUTH_SECRET": secrets.token_urlsafe(32),
            }
        )
        execute(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name + "-cli",
                "--network",
                name,
                "--env",
                "AUTH_DATABASE_URL",
                "--env",
                "BETTER_AUTH_URL",
                "--env",
                "BETTER_AUTH_SECRET",
                "--entrypoint",
                "/opt/ba-cli/node_modules/.bin/better-auth",
                image,
                "migrate",
                "--config",
                "src/lib/auth/auth.cli.ts",
                "--yes",
            ],
            cwd=ROOT,
            env=env,
            # CLIの失敗出力にも一時DBのパスワードを残さない。
            log=os.devnull,
            timeout=180,
        )
        execute(
            [
                "docker",
                "exec",
                name,
                "pg_dump",
                "-U",
                "vector",
                "-d",
                "vector",
                "--schema=auth",
                "--schema-only",
                "--no-owner",
                "--no-privileges",
            ],
            cwd=ROOT,
            log=directory / "auth.sql",
        )
    finally:
        # 名前はこの実行専用に固定し、失敗途中のローカルコンテナも回収する。
        for container in [name + "-cli", name]:
            subprocess.run(  # noqa: S603,S607
                ["docker", "rm", "-fv", container],  # noqa: S607
                capture_output=True,
                timeout=30,
            )
        if network_created:
            execute(
                ["docker", "network", "rm", name],
                cwd=ROOT,
                log=directory / "auth-network-cleanup.log",
            )
