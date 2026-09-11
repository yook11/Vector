"""実ロールの許可一覧と、それ以外の操作が拒否されることを確認する。"""

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

ROLES = ("vector_auth", "vector_app", "vector_collect")
DML = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
TABLE_OPERATIONS = sorted(DML | {"TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"})
COLUMN_OPERATIONS = ["SELECT", "INSERT", "UPDATE", "REFERENCES"]
COLLECT_TABLE_PERMISSIONS = {
    "news_sources": frozenset({"SELECT"}),
    "analyzable_articles": frozenset({"SELECT", "INSERT"}),
    "incomplete_articles": DML,
    "pipeline_events": frozenset({"INSERT"}),
    "outbox_events": frozenset({"INSERT"}),
}
COLLECT_READABLE_COLUMNS = {
    "pipeline_events": frozenset({"id", "occurred_at"}),
    "outbox_events": frozenset(
        {
            "event_id",
            "schema_version",
            "occurred_at",
            "next_attempt_at",
            "attempt_count",
        }
    ),
}
COLLECT_SEQUENCE_TABLES = frozenset(
    {"analyzable_articles", "incomplete_articles", "pipeline_events"}
)
# y2の既存許可を保持し、将来のsequenceへSELECT・UPDATEを自動許可しない。
APP_SEQUENCE_READ_WRITE_TABLES = frozenset(
    {
        "agent_message_sources",
        "analyzable_articles",
        "analyzed_articles",
        "article_curations",
        "curation_noises",
        "incomplete_articles",
        "categories",
        "news_sources",
        "out_of_scope_articles",
        "pipeline_events",
        "query_embedding_cache",
        "weekly_briefings",
    }
)


def _allowed_table_operations(role, schema, table):
    if role == "vector_auth" and schema == "auth":
        return DML
    if role == "vector_app":
        if schema == "public":
            return DML
        if (schema, table) == ("auth", "user"):
            return frozenset({"SELECT", "REFERENCES"})
    if role == "vector_collect" and schema == "public":
        return COLLECT_TABLE_PERMISSIONS.get(table, frozenset())
    return frozenset()


@pytest.mark.parametrize("role", ROLES)
async def test_table_permissions_match_allowlist(system_database, role):
    """未列挙のテーブルを含め、過剰権限と不足権限を同時に検出する。"""
    async with system_database.connect(role) as connection:
        rows = await connection.fetch(
            "SELECT n.nspname AS schema, c.relname AS name, p.operation, "
            "has_table_privilege(c.oid, p.operation) AS granted, "
            "has_table_privilege(c.oid, p.operation || ' WITH GRANT OPTION') AS "
            "delegable "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN unnest($1::text[]) AS p(operation) "
            "WHERE n.nspname IN ('public','auth') AND c.relkind IN "
            "('r','p','v','m','f')",
            TABLE_OPERATIONS,
        )
    objects = {(r["schema"], r["name"]) for r in rows}
    assert {("auth", "user"), ("auth", "account")} <= objects
    assert {("public", table) for table in COLLECT_TABLE_PERMISSIONS} <= objects
    for row in rows:
        expected = row["operation"] in _allowed_table_operations(
            role, row["schema"], row["name"]
        )
        assert row["granted"] is expected, (role, dict(row), expected)
        assert not row["delegable"], (role, dict(row))


@pytest.mark.parametrize("role", ROLES)
async def test_column_permissions_match_allowlist(system_database, role):
    """テーブル権限の継承と列単位の追加許可を含む実効権限を確認する。"""
    async with system_database.connect(role) as connection:
        rows = await connection.fetch(
            "SELECT n.nspname AS schema, c.relname AS name, a.attname AS column, "
            "p.operation, has_column_privilege(c.oid, a.attnum, p.operation) AS "
            "granted, "
            "has_column_privilege(c.oid, a.attnum, "
            "p.operation || ' WITH GRANT OPTION') AS delegable "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT "
            "a.attisdropped "
            "CROSS JOIN unnest($1::text[]) AS p(operation) "
            "WHERE n.nspname IN ('public','auth') AND c.relkind IN "
            "('r','p','v','m','f')",
            COLUMN_OPERATIONS,
        )
    columns = {(r["schema"], r["name"], r["column"]) for r in rows}
    assert columns
    for table, names in COLLECT_READABLE_COLUMNS.items():
        assert {("public", table, name) for name in names} <= columns
    for row in rows:
        expected = row["operation"] in _allowed_table_operations(
            role, row["schema"], row["name"]
        )
        if (
            role == "vector_collect"
            and row["schema"] == "public"
            and row["operation"] == "SELECT"
        ):
            expected |= row["column"] in COLLECT_READABLE_COLUMNS.get(row["name"], ())
        assert row["granted"] is expected, (role, dict(row), expected)
        assert not row["delegable"], (role, dict(row))


@pytest.mark.parametrize("role", ROLES)
async def test_sequence_permissions_match_allowlist(system_database, role):
    """sequenceの所有テーブルを基準に採番と既存の追加許可を確認する。"""
    async with system_database.connect(role) as connection:
        rows = await connection.fetch(
            "SELECT n.nspname AS schema, s.relname AS name, t.relname AS owner_table, "
            "p.operation, has_sequence_privilege(s.oid, p.operation) AS granted, "
            "has_sequence_privilege(s.oid, p.operation || ' WITH GRANT OPTION') AS "
            "delegable "
            "FROM pg_class s JOIN pg_namespace n ON n.oid=s.relnamespace "
            "LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=s.oid "
            "AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') "
            "LEFT JOIN pg_class t ON t.oid=d.refobjid "
            "CROSS JOIN unnest(ARRAY['USAGE','SELECT','UPDATE']) AS p(operation) "
            "WHERE s.relkind='S' AND n.nspname IN ('public','auth')"
        )
    assert COLLECT_SEQUENCE_TABLES <= {
        r["owner_table"] for r in rows if r["schema"] == "public"
    }
    for row in rows:
        allowed = set()
        if role == "vector_auth" and row["schema"] == "auth":
            allowed = {"USAGE"}
        elif role == "vector_app" and row["schema"] == "public":
            allowed = {"USAGE"}
            if row["owner_table"] in APP_SEQUENCE_READ_WRITE_TABLES:
                allowed |= {"SELECT", "UPDATE"}
        elif (
            role == "vector_collect"
            and row["schema"] == "public"
            and row["owner_table"] in COLLECT_SEQUENCE_TABLES
        ):
            allowed = {"USAGE"}
        assert row["granted"] is (row["operation"] in allowed), (
            role,
            dict(row),
            allowed,
        )
        assert not row["delegable"], (role, dict(row))


@pytest.mark.parametrize("role", ROLES)
async def test_runtime_role_cannot_administer_database(system_database, role):
    """管理権限・他ロールへの切替・アプリschema内のDDLを許可しない。"""
    async with system_database.connect(role) as connection:
        assert await connection.fetchval("SELECT current_user") == role
        assert await connection.fetchval("SELECT session_user") == role
        flags = await connection.fetchrow(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication "
            "FROM pg_roles WHERE rolname=current_user"
        )
        assert not any(flags.values())
        for other in ("vector", *ROLES):
            if other != role:
                assert not await connection.fetchval(
                    "SELECT pg_has_role($1, 'SET')", other
                )
        for schema in ("public", "auth"):
            assert not await connection.fetchval(
                "SELECT has_schema_privilege($1, 'CREATE')", schema
            )
        required = (
            ("public", "auth")
            if role == "vector_app"
            else (("auth",) if role == "vector_auth" else ("public",))
        )
        for schema in required:
            assert await connection.fetchval(
                "SELECT has_schema_privilege($1, 'USAGE')", schema
            )


@pytest.mark.parametrize(
    ("role", "statement"),
    [
        ("vector_app", "CREATE TABLE public.permission_forbidden (id integer)"),
        ("vector_auth", "SELECT * FROM public.watchlist_entries"),
        ("vector_app", "SELECT * FROM auth.account"),
        (
            "vector_app",
            'INSERT INTO auth."user" (id, name, email, '
            '"emailVerified", "createdAt", "updatedAt", role) '
            "VALUES (gen_random_uuid(), 'test', 'permission@example.com', "
            "false, now(), now(), 'user')",
        ),
        ("vector_app", 'UPDATE auth."user" SET role=role WHERE false'),
        ("vector_app", 'DELETE FROM auth."user" WHERE false'),
        ("vector_app", 'DELETE FROM auth."rateLimit" WHERE false'),
        ("vector_collect", 'SELECT id FROM auth."user"'),
        ("vector_collect", "SELECT * FROM public.article_curations"),
        ("vector_collect", "SELECT payload FROM public.pipeline_events"),
        ("vector_collect", "SELECT payload FROM public.outbox_events"),
        (
            "vector_collect",
            "INSERT INTO public.news_sources (name) VALUES ('permission-test')",
        ),
        ("vector_collect", "UPDATE public.news_sources SET name=name WHERE false"),
        ("vector_collect", "DELETE FROM public.news_sources WHERE false"),
        (
            "vector_collect",
            "UPDATE public.analyzable_articles SET original_title=original_title "
            "WHERE false",
        ),
        ("vector_collect", "DELETE FROM public.analyzable_articles WHERE false"),
        (
            "vector_collect",
            "UPDATE public.pipeline_events SET payload=payload WHERE false",
        ),
        ("vector_collect", "DELETE FROM public.pipeline_events WHERE false"),
        (
            "vector_collect",
            "UPDATE public.outbox_events SET published_at=now() WHERE false",
        ),
        ("vector_collect", "DELETE FROM public.outbox_events WHERE false"),
        ("vector_collect", "TRUNCATE public.pipeline_events"),
        ("vector_collect", "TRUNCATE public.outbox_events"),
    ],
)
async def test_forbidden_operation_raises_permission_error(
    system_database, role, statement
):
    """権限不足以外のSQLエラーを禁止操作の成功と取り違えない。"""
    async with system_database.connect(role) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement)


async def test_app_can_commit_category_crud(system_database):
    """自分で追加したカテゴリの読取・更新・削除を別接続から確認する。"""
    async with system_database.connect("vector_app") as connection:
        category_id = await connection.fetchval(
            "INSERT INTO categories (slug,name) VALUES ('permissions','before') "
            "RETURNING id"
        )
    async with system_database.connect("vector_app") as connection:
        assert (
            await connection.fetchval(
                "SELECT name FROM categories WHERE id=$1", category_id
            )
            == "before"
        )
        assert (
            await connection.execute(
                "UPDATE categories SET name='after' WHERE id=$1", category_id
            )
            == "UPDATE 1"
        )
    async with system_database.connect("vector_app") as connection:
        assert (
            await connection.fetchval(
                "SELECT name FROM categories WHERE id=$1", category_id
            )
            == "after"
        )
        assert (
            await connection.execute("DELETE FROM categories WHERE id=$1", category_id)
            == "DELETE 1"
        )
    async with system_database.connect("vector_app") as connection:
        assert not await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM categories WHERE id=$1)", category_id
        )


@pytest.mark.parametrize("role", ("vector_auth", "vector_app"))
async def test_runtime_can_read_auth_user(system_database, role):
    """必要なAuthユーザー参照を実ロールのSELECTで確認する。"""
    async with system_database.connect(role) as connection:
        await connection.fetch('SELECT id FROM auth."user" LIMIT 1')


async def test_auth_can_delete_rate_limit(system_database):
    """Authの保持期限処理に必要なDELETE権限を実操作で確認する。"""
    async with system_database.connect("vector_auth") as connection:
        assert (
            await connection.execute('DELETE FROM auth."rateLimit" WHERE false')
            == "DELETE 0"
        )


async def test_collect_can_commit_article_and_events_with_returning(system_database):
    """収集ロールで記事・監査・Outboxを追加し、採番と既定値を取得する。"""
    async with system_database.connect("vector_collect") as connection:
        source = await connection.fetchrow(
            "SELECT id,name FROM news_sources ORDER BY id LIMIT 1"
        )
        assert source is not None
        async with connection.transaction():
            article_id = await connection.fetchval(
                "INSERT INTO analyzable_articles "
                "(source_id,source_url,original_title,original_content,published_at) "
                "VALUES "
                "($1,'https://example.com/permission-article','title','content',now()) "
                "RETURNING id",
                source["id"],
            )
            assert (
                await connection.fetchval(
                    "SELECT original_title FROM analyzable_articles WHERE id=$1",
                    article_id,
                )
                == "title"
            )
            event = await connection.fetchrow(
                "INSERT INTO pipeline_events "
                "(stage,event_type,outcome_code,article_id) "
                "VALUES ('acquisition','succeeded','permission_test',$1) RETURNING "
                "id,occurred_at",
                article_id,
            )
            outbox = await connection.fetchrow(
                "INSERT INTO outbox_events (event_type,payload) VALUES "
                "('article.acquired','{}') "
                "RETURNING "
                "event_id,schema_version,occurred_at,next_attempt_at,attempt_count"
            )
            assert event["id"] and event["occurred_at"]
            assert (
                outbox["event_id"]
                and outbox["occurred_at"]
                and outbox["next_attempt_at"]
            )
            assert outbox["schema_version"] == 1 and outbox["attempt_count"] == 0
    async with system_database.connect("vector_app") as connection:
        assert (
            await connection.fetchval(
                "SELECT article_id FROM pipeline_events WHERE id=$1", event["id"]
            )
            == article_id
        )
        assert (
            await connection.fetchval(
                "SELECT event_type FROM outbox_events WHERE event_id=$1",
                outbox["event_id"],
            )
            == "article.acquired"
        )


async def test_collect_can_crud_incomplete_article(system_database):
    """未完成記事の採番と読取・更新・削除を収集ロールで実行する。"""
    async with system_database.connect("vector_collect") as connection:
        source = await connection.fetchrow(
            "SELECT id,name FROM news_sources ORDER BY id LIMIT 1"
        )
        assert source is not None
        article_id = await connection.fetchval(
            "INSERT INTO incomplete_articles "
            "(url,source_id,source_name,status,observed_article,ready_at) "
            "VALUES "
            "('https://example.com/permission-incomplete',$1,$2,'open','{}',now()) "
            "RETURNING id",
            source["id"],
            source["name"],
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM incomplete_articles WHERE id=$1", article_id
            )
            == "open"
        )
        assert (
            await connection.execute(
                "UPDATE incomplete_articles SET status='closed',ready_at=NULL "
                "WHERE id=$1",
                article_id,
            )
            == "UPDATE 1"
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM incomplete_articles WHERE id=$1", article_id
            )
            == "closed"
        )
        assert (
            await connection.execute(
                "DELETE FROM incomplete_articles WHERE id=$1", article_id
            )
            == "DELETE 1"
        )
        assert not await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM incomplete_articles WHERE id=$1)", article_id
        )
