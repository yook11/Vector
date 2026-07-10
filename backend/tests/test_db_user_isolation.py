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

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("role_db")]


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


async def _require_alembic_applied_schema(conn: asyncpg.Connection) -> None:
    """alembic migration が適用済の schema (public.watchlist_entries +
    auth schema GRANT) が無いと、本ファイルのテストは role 権限ではなく
    UndefinedTableError で fail する。``make test-integration`` の流れは
    conftest の ``metadata.create_all()`` に依存し alembic を流さないため、
    その環境ではこのチェックが skip を発火する。本来の実行経路 (docker
    compose backend container 内 + alembic upgrade head) では proceed する。
    """
    # pg_tables は誰でも (vector_auth で public 権限が無くても) read できる
    # catalog view なので、role permission に依存せず table の有無を判定できる。
    # (to_regclass(...) IS NOT NULL は SELECT 権限不足時も NULL を返すため不可。)
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_tables "
        "WHERE schemaname = 'public' AND tablename = 'watchlist_entries')"
    )
    if not exists:
        pytest.skip("alembic-applied schema required (public.watchlist_entries)")


async def _require_auth_rate_limit_table(conn: asyncpg.Connection) -> None:
    """Better Auth CLI 管理の auth.rateLimit table が無い環境では skip する。"""
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_tables "
        "WHERE schemaname = 'auth' AND tablename = 'rateLimit')"
    )
    if not exists:
        pytest.skip('Better Auth schema required (auth."rateLimit")')


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
        await _require_alembic_applied_schema(conn)
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
        await _require_alembic_applied_schema(conn)
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def collect_conn():
    """vector_collect role での asyncpg 接続を提供する (本番 vector db)。"""
    if settings.postgres_collect_password is None:
        pytest.skip("POSTGRES_COLLECT_PASSWORD not configured")
    conn = await asyncpg.connect(
        **_connection_kwargs(
            "vector_collect", settings.postgres_collect_password.get_secret_value()
        )
    )
    try:
        await _require_alembic_applied_schema(conn)
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

    async def test_can_delete_auth_rate_limit(self, auth_conn) -> None:
        """vector_auth は auth.rateLimit retention 用に DELETE できる。"""
        await _require_auth_rate_limit_table(auth_conn)
        result = await auth_conn.execute('DELETE FROM auth."rateLimit" WHERE false')
        assert result == "DELETE 0"


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

    async def test_cannot_delete_auth_rate_limit(self, app_conn) -> None:
        """vector_app は auth.rateLimit を DELETE できない。"""
        await _require_auth_rate_limit_table(app_conn)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute('DELETE FROM auth."rateLimit" WHERE false')

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


class TestVectorCollectIsolation:
    """vector_collect (collect worker 専用) が acquisition+completion の触る 4 table
    だけに最小権限で閉じ込められていることを構造的に保証する。

    collect は analyzable_articles / pipeline_events を DELETE できない。
    正の write 権限は副作用ゼロの has_*_privilege catalog 関数で検証する。
    """

    async def test_can_select_news_sources(self, collect_conn) -> None:
        """vector_collect は news_sources を SELECT できる (dispatch が読む)。"""
        rows = await collect_conn.fetch("SELECT id FROM public.news_sources LIMIT 1")
        assert isinstance(rows, list)

    async def test_can_select_articles(self, collect_conn) -> None:
        """vector_collect は analyzable_articles を SELECT できる。"""
        rows = await collect_conn.fetch(
            "SELECT id FROM public.analyzable_articles LIMIT 1"
        )
        assert isinstance(rows, list)

    async def test_can_select_incomplete_articles(self, collect_conn) -> None:
        """vector_collect は incomplete_articles を SELECT できる。"""
        rows = await collect_conn.fetch(
            "SELECT id FROM public.incomplete_articles LIMIT 1"
        )
        assert isinstance(rows, list)

    async def test_has_full_dml_on_incomplete_articles(self, collect_conn) -> None:
        """incomplete_articles は SELECT/INSERT/UPDATE/DELETE すべて持つ
        (completion の lease 状態機械が全 DML を要する)。"""
        granted = await collect_conn.fetchval(
            "SELECT has_table_privilege('public.incomplete_articles', 'SELECT') "
            "AND has_table_privilege('public.incomplete_articles', 'INSERT') "
            "AND has_table_privilege('public.incomplete_articles', 'UPDATE') "
            "AND has_table_privilege('public.incomplete_articles', 'DELETE')"
        )
        assert granted is True

    async def test_can_insert_articles(self, collect_conn) -> None:
        """analyzable_articles は INSERT を持つ (両 stage が新規記事を INSERT する)。"""
        granted = await collect_conn.fetchval(
            "SELECT has_table_privilege('public.analyzable_articles', 'INSERT')"
        )
        assert granted is True

    async def test_cannot_update_or_delete_articles(self, collect_conn) -> None:
        """analyzable_articles は UPDATE/DELETE を持たない。"""
        denied = await collect_conn.fetchval(
            "SELECT has_table_privilege('public.analyzable_articles', 'UPDATE') "
            "OR has_table_privilege('public.analyzable_articles', 'DELETE')"
        )
        assert denied is False

    async def test_can_insert_pipeline_events(self, collect_conn) -> None:
        """pipeline_events は INSERT を持つ (監査 append-only)。"""
        granted = await collect_conn.fetchval(
            "SELECT has_table_privilege('public.pipeline_events', 'INSERT')"
        )
        assert granted is True

    async def test_can_select_id_and_occurred_at_on_pipeline_events(
        self, collect_conn
    ) -> None:
        """pipeline_events は RETURNING に出る id, occurred_at の 2 列だけ SELECT
        できる (ORM session.add の INSERT...RETURNING に必要)。"""
        granted = await collect_conn.fetchval(
            "SELECT has_column_privilege('public.pipeline_events', 'id', 'SELECT') "
            "AND has_column_privilege("
            "'public.pipeline_events', 'occurred_at', 'SELECT')"
        )
        assert granted is True

    async def test_cannot_select_payload_on_pipeline_events(self, collect_conn) -> None:
        """pipeline_events.payload (本文 / 外部入力) は読めず append-only 性を維持。"""
        granted = await collect_conn.fetchval(
            "SELECT has_column_privilege('public.pipeline_events', 'payload', 'SELECT')"
        )
        assert granted is False

    async def test_has_sequence_usage_for_inserted_tables(self, collect_conn) -> None:
        """INSERT する 3 table の id sequence に USAGE を持つ (serial 採番に必要)。"""
        granted = await collect_conn.fetchval(
            "SELECT has_sequence_privilege("
            "pg_get_serial_sequence('public.incomplete_articles', 'id'), 'USAGE') "
            "AND has_sequence_privilege("
            "pg_get_serial_sequence('public.analyzable_articles', 'id'), 'USAGE') "
            "AND has_sequence_privilege("
            "pg_get_serial_sequence('public.pipeline_events', 'id'), 'USAGE')"
        )
        assert granted is True

    async def test_cannot_select_analysis_table(self, collect_conn) -> None:
        """analysis BC の article_curations は SELECT で権限拒否される
        (collect 侵害から分析結果を遮断)。"""
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await collect_conn.fetch("SELECT * FROM public.article_curations LIMIT 1")

    async def test_cannot_access_auth_schema(self, collect_conn) -> None:
        """auth schema は USAGE すら無く auth.user SELECT で権限拒否される。"""
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await collect_conn.fetch('SELECT id FROM auth."user" LIMIT 1')
