"""実行ロール全体に共通する認証主体・管理権限の境界を確認する。"""

import pytest

ROLES = (
    "vector_auth",
    "vector_app",
    "vector_collect",
    "vector_outbox_relay",
    "vector_auth_rate_limit_cleanup",
)
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("role", ROLES)
async def test_connection_authenticates_as_requested_role(system_database, role):
    """管理者のSET ROLEではなく各実行ロール自身で認証する。"""
    async with system_database.connect(role) as connection:
        actual = await connection.fetchrow("SELECT current_user, session_user")
    assert tuple(actual) == (role, role)


@pytest.mark.parametrize("role", ROLES)
async def test_runtime_has_no_admin_attributes(system_database, role):
    """実行ロールはDB管理やRLS迂回の属性を持たない。"""
    async with system_database.connect(role) as connection:
        actual = await connection.fetchrow(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication "
            "FROM pg_roles WHERE rolname=current_user"
        )
    assert dict(actual) == {
        "rolsuper": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolbypassrls": False,
        "rolreplication": False,
    }


@pytest.mark.parametrize("role", ROLES)
async def test_runtime_cannot_assume_owner_or_other_runtime_roles(
    system_database, role
):
    """実行ロールは所有者や他のアプリへ認証主体を切り替えられない。"""
    async with system_database.connect(role) as connection:
        for other in ("vector", *ROLES):
            if other != role:
                assert not await connection.fetchval(
                    "SELECT pg_has_role($1, 'SET')", other
                ), (role, other)


@pytest.mark.parametrize("role", ROLES)
async def test_runtime_cannot_create_objects_in_application_schemas(
    system_database, role
):
    """実行ロールにpublic・auth内のオブジェクト作成を許可しない。"""
    async with system_database.connect(role) as connection:
        for schema in ("public", "auth"):
            assert not await connection.fetchval(
                "SELECT has_schema_privilege($1, 'CREATE')", schema
            ), (role, schema)


@pytest.mark.parametrize(
    ("role", "schemas"),
    [
        ("vector_auth", ("auth",)),
        ("vector_app", ("public", "auth")),
        ("vector_collect", ("public",)),
        ("vector_outbox_relay", ("public",)),
        ("vector_auth_rate_limit_cleanup", ("auth",)),
    ],
)
async def test_runtime_can_use_required_schemas(system_database, role, schemas):
    """実行ロールは業務に必要なschemaを利用できる。"""
    async with system_database.connect(role) as connection:
        for schema in schemas:
            assert await connection.fetchval(
                "SELECT has_schema_privilege($1, 'USAGE')", schema
            ), (role, schema)
