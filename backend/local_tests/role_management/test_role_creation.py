"""ロール管理を実PostgreSQLで検証する。"""

from uuid import uuid4

import pytest

from scripts.db_role_runner import RoleContractError, apply_roles


@pytest.fixture
async def role_database(system_database):
    async with system_database.connect("vector") as connection:
        group_created = not await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam')"
        )
        if group_created:
            await connection.execute("CREATE ROLE rds_iam NOLOGIN")
        names = (f"vector_role_test_{uuid4().hex}", f"vector_role_test_{uuid4().hex}")
        try:
            yield connection, names
        finally:
            for name in names:
                await connection.execute(f'DROP ROLE IF EXISTS "{name}"')
            if group_created:
                await connection.execute("DROP ROLE rds_iam")


@pytest.mark.asyncio
async def test_create_iam_login_without_administrative_privileges(role_database):
    """作成した接続ロールはIAM所属だけを持つ。"""
    connection, (name, _) = role_database
    assert await apply_roles(connection, (name,)) == (name,)
    role = await connection.fetchrow(
        """SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                  rolreplication, rolbypassrls,
                  pg_has_role(oid, 'rds_iam', 'MEMBER') AS iam
           FROM pg_roles WHERE rolname = $1""",
        name,
    )
    assert dict(role) == {
        "rolcanlogin": True,
        "rolsuper": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
        "iam": True,
    }


@pytest.mark.asyncio
async def test_matching_existing_role_is_unchanged(role_database):
    """再実行時に同じロールを維持する。"""
    connection, (name, _) = role_database
    await apply_roles(connection, (name,))
    original_oid = await connection.fetchval(
        "SELECT oid FROM pg_roles WHERE rolname = $1", name
    )
    assert await apply_roles(connection, (name,)) == ()
    assert (
        await connection.fetchval("SELECT oid FROM pg_roles WHERE rolname = $1", name)
        == original_oid
    )


@pytest.mark.asyncio
async def test_unsafe_existing_role_rolls_back_other_role_creation(role_database):
    """既存の管理ロールを拒否し、先に作成したロールも残さない。"""
    connection, (new_role, unsafe_role) = role_database
    await connection.execute(f'CREATE ROLE "{unsafe_role}" LOGIN CREATEROLE')
    with pytest.raises(RoleContractError, match="existing_role_attributes_mismatch"):
        await apply_roles(connection, (new_role, unsafe_role))
    assert not await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = $1)", new_role
    )
    assert await connection.fetchval(
        "SELECT rolcreaterole FROM pg_roles WHERE rolname = $1", unsafe_role
    )


@pytest.mark.asyncio
async def test_existing_role_missing_iam_is_not_silently_changed(role_database):
    """IAM所属のない既存ロールは変更せず停止する。"""
    connection, (name, _) = role_database
    await connection.execute(f'CREATE ROLE "{name}" LOGIN')
    with pytest.raises(RoleContractError, match="existing_role_memberships_mismatch"):
        await apply_roles(connection, (name,))
    assert not await connection.fetchval(
        "SELECT pg_has_role($1, 'rds_iam', 'MEMBER')", name
    )


@pytest.mark.asyncio
async def test_migration_lock_prevents_role_creation(role_database, system_database):
    """migration実行中はロールを作成しない。"""
    from scripts.db_role_runner import MIGRATION_LOCK_KEY

    connection, (name, _) = role_database
    async with system_database.connect("vector") as migration:
        await migration.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        try:
            with pytest.raises(RoleContractError, match="migration_lock_unavailable"):
                await apply_roles(connection, (name,))
            assert not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = $1)", name
            )
        finally:
            await migration.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)


@pytest.mark.asyncio
async def test_existing_object_owner_is_rejected(role_database):
    """オブジェクト所有による権限を持つ既存ロールは受け入れない。"""
    connection, (name, _) = role_database
    await apply_roles(connection, (name,))
    await connection.execute("CREATE TABLE public.role_management_probe (id integer)")
    await connection.execute(
        f'ALTER TABLE public.role_management_probe OWNER TO "{name}"'
    )
    try:
        with pytest.raises(RoleContractError, match="existing_role_owns_objects"):
            await apply_roles(connection, (name,))
    finally:
        await connection.execute("DROP TABLE public.role_management_probe")
