"""承認されたmanifestのIAM接続ロールを作成する。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import sys
from pathlib import Path
from typing import Literal

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_LOCK_KEY = 0x564543544F52
PROTECTED_ROLES = frozenset(
    {"vector_app", "vector_auth", "vector_collect", "vector_master"}
)


class RoleContractError(ValueError):
    """入力値を含まない契約違反。"""


def validate_role_name(name: str) -> str:
    if (
        not re.fullmatch(r"vector_[a-z][a-z0-9_]{0,55}", name)
        or name in PROTECTED_ROLES
    ):
        raise RoleContractError("invalid_role_name")
    return name


class RoleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    protocol_version: Literal[1]
    roles: list[str] = Field(min_length=1)

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, roles: list[str]) -> list[str]:
        for role in roles:
            validate_role_name(role)
        if len(set(roles)) != len(roles):
            raise RoleContractError("duplicate_roles")
        return roles


class RoleRunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", hide_input_in_errors=True
    )
    db_admin_host: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9.-]+$")
    db_admin_port: int = Field(default=5432, ge=1, le=65535)
    db_admin_database: Literal["vector"] = "vector"
    db_admin_user: Literal["vector_master"] = "vector_master"
    db_admin_password: SecretStr = Field(min_length=1)
    db_roles_release_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    db_roles_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def load_manifest(path: Path, expected_sha256: str) -> tuple[str, ...]:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise RoleContractError("manifest_digest_mismatch")
    return tuple(RoleManifest.model_validate_json(content).roles)


async def _verify_role(connection: asyncpg.Connection, name: str) -> bool:
    role = await connection.fetchrow(
        """SELECT oid, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                  rolreplication, rolbypassrls, rolinherit, rolconnlimit, rolvaliduntil
           FROM pg_roles WHERE rolname = $1""",
        name,
    )
    if role is None:
        return False
    if (
        not role["rolcanlogin"]
        or not role["rolinherit"]
        or any(
            role[key]
            for key in (
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        )
        or role["rolconnlimit"] != -1
        or role["rolvaliduntil"] is not None
    ):
        raise RoleContractError("existing_role_attributes_mismatch")
    memberships = await connection.fetch(
        """SELECT parent.rolname, m.admin_option FROM pg_auth_members m
           JOIN pg_roles parent ON parent.oid = m.roleid WHERE m.member = $1""",
        role["oid"],
    )
    if [(row["rolname"], row["admin_option"]) for row in memberships] != [
        ("rds_iam", False)
    ]:
        raise RoleContractError("existing_role_memberships_mismatch")
    if await connection.fetchval(
        """SELECT EXISTS(SELECT 1 FROM pg_shdepend
           WHERE refclassid = 'pg_authid'::regclass
             AND refobjid = $1 AND deptype = 'o')""",
        role["oid"],
    ):
        raise RoleContractError("existing_role_owns_objects")
    if await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_db_role_setting WHERE setrole = $1)",
        role["oid"],
    ):
        raise RoleContractError("existing_role_settings_mismatch")
    return True


async def apply_roles(
    connection: asyncpg.Connection, roles: tuple[str, ...]
) -> tuple[str, ...]:
    """全対象を同一transactionで検証し、新しいロールだけ作成する。"""
    RoleManifest(protocol_version=1, roles=list(roles))
    created = []
    async with asyncio.timeout(60), connection.transaction():
        await connection.execute("SET LOCAL lock_timeout = '5s'")
        await connection.execute("SET LOCAL statement_timeout = '5s'")
        if not await connection.fetchval(
            "SELECT pg_try_advisory_xact_lock($1)", MIGRATION_LOCK_KEY
        ):
            raise RoleContractError("migration_lock_unavailable")
        for role in roles:
            if await _verify_role(connection, role):
                continue
            create = await connection.fetchval(
                "SELECT format('CREATE ROLE %I LOGIN INHERIT NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', $1::text)",
                role,
            )
            await connection.execute(create)
            grant = await connection.fetchval(
                "SELECT format('GRANT rds_iam TO %I', $1::text)", role
            )
            await connection.execute(grant)
            await _verify_role(connection, role)
            created.append(role)
    return tuple(created)


async def run(settings: RoleRunnerSettings) -> tuple[str, ...]:
    roles = load_manifest(ROOT / "db_roles.json", settings.db_roles_manifest_sha256)
    tls = ssl.create_default_context(
        cafile=str(ROOT / "app/db/rds-ca-ap-northeast-1.pem")
    )
    async with asyncio.timeout(60):
        connection = await asyncpg.connect(
            host=settings.db_admin_host,
            port=settings.db_admin_port,
            database=settings.db_admin_database,
            user=settings.db_admin_user,
            password=settings.db_admin_password.get_secret_value(),
            ssl=tls,
            timeout=10,
            command_timeout=5,
        )
        try:
            identity = await connection.fetchrow(
                "SELECT current_database() AS db, current_user AS username, "
                "session_user AS session"
            )
            if dict(identity) != {
                "db": settings.db_admin_database,
                "username": settings.db_admin_user,
                "session": settings.db_admin_user,
            }:
                raise RoleContractError("database_identity_mismatch")
            return await apply_roles(connection, roles)
        finally:
            await connection.close(timeout=5)


def main() -> int:
    try:
        settings = RoleRunnerSettings()
        created = asyncio.run(run(settings))
    except Exception:
        print(
            json.dumps({"result": "failed", "reason": "role_management_failed"}),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "result": "applied",
                "release_sha": settings.db_roles_release_sha,
                "manifest_sha256": settings.db_roles_manifest_sha256,
                "created_roles": created,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
