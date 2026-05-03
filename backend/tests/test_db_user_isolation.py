"""Postgres user 分離 (red-team AUTH-N4) の権限境界を振る舞いで検証する。

vector_auth (auth.* DML) と vector_app (public.* DML + auth.user 参照のみ)
の 2 application role が、想定外の schema へのアクセスで `InsufficientPrivilege`
を確実に raise することを構造的に保証する。

migration role (vector) で動く既存テストでは権限境界が再現できないため、
本ファイルは asyncpg.connect で各 user の生 connection を本番 db に開いて
assert する。テスト内の書き込みは ``isolationtest`` slug 限定 + 即 DELETE
で副作用ゼロにする。
"""

from __future__ import annotations

import asyncpg
import pytest

from app.config import settings

pytestmark = pytest.mark.asyncio


def _connection_kwargs(user: str, password: str) -> dict[str, object]:
    """compose 内の db host (db:5432) で asyncpg 接続するパラメータを返す。"""
    raw = (settings.migration_database_url or settings.database_url).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    # postgresql://vector:pw@db:5432/vector → host=db, port=5432, db=vector を抽出
    after_at = raw.split("@", 1)[1]
    host_port, dbname = after_at.split("/", 1)
    host, port = host_port.split(":")
    return {
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "database": dbname,
    }


@pytest.fixture
async def auth_conn():
    """vector_auth role での asyncpg 接続を提供する (本番 vector db)。"""
    if settings.postgres_auth_password is None:
        pytest.skip("POSTGRES_AUTH_PASSWORD not configured")
    conn = await asyncpg.connect(
        **_connection_kwargs(
            "vector_auth", settings.postgres_auth_password.get_secret_value()
        )
    )
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def app_conn():
    """vector_app role での asyncpg 接続を提供する (本番 vector db)。"""
    if settings.postgres_app_password is None:
        pytest.skip("POSTGRES_APP_PASSWORD not configured")
    conn = await asyncpg.connect(
        **_connection_kwargs(
            "vector_app", settings.postgres_app_password.get_secret_value()
        )
    )
    try:
        yield conn
    finally:
        await conn.close()


class TestVectorAuthIsolation:
    async def test_cannot_select_from_public_table(self, auth_conn) -> None:
        """vector_auth は public.watchlist_entries SELECT で権限拒否される。"""
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await auth_conn.fetch("SELECT * FROM public.watchlist_entries LIMIT 1")

    async def test_can_select_auth_user(self, auth_conn) -> None:
        """vector_auth は auth.user に SELECT できる (Better Auth runtime のため)。"""
        rows = await auth_conn.fetch('SELECT id FROM auth."user" LIMIT 1')
        assert isinstance(rows, list)


class TestVectorAppIsolation:
    async def test_cannot_insert_into_auth_user(self, app_conn) -> None:
        """vector_app は auth.user に INSERT で権限拒否される。"""
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(
                'INSERT INTO auth."user" (id, email, "emailVerified", '
                '"createdAt", "updatedAt") '
                "VALUES ('00000000-0000-0000-0000-000000000999', "
                "'attacker@example.com', true, now(), now())"
            )

    async def test_can_select_auth_user_for_fk(self, app_conn) -> None:
        """vector_app は auth.user に SELECT できる (FK 整合性確認に必要)。"""
        rows = await app_conn.fetch('SELECT id FROM auth."user" LIMIT 1')
        assert isinstance(rows, list)

    async def test_can_crud_public_categories(self, app_conn) -> None:
        """vector_app は public.* に CRUD できる (本来の application 経路)。

        副作用ゼロ化: ``isolationtest`` slug 限定で INSERT → SELECT → DELETE。
        """
        await app_conn.execute(
            "INSERT INTO public.categories (slug, name) "
            "VALUES ('isolationtest', 'isolationtest') "
            "ON CONFLICT (slug) DO NOTHING"
        )
        try:
            rows = await app_conn.fetch(
                "SELECT id FROM public.categories WHERE slug = 'isolationtest'"
            )
            assert len(rows) == 1
        finally:
            await app_conn.execute(
                "DELETE FROM public.categories WHERE slug = 'isolationtest'"
            )
