"""収集ロールの保存権限と既存記事・監査・イベントの保護を確認する。"""

import asyncpg
import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_owners,
    read_sequence_permissions,
    read_table_columns,
    read_table_permissions,
    read_tables,
)

pytestmark = pytest.mark.asyncio


async def test_collect_has_only_collection_table_permissions(system_database):
    """収集ロールの表権限は保存先ごとの許可一覧と完全一致する。"""
    expected = {
        ("public", "news_sources", "SELECT"),
        ("public", "analyzable_articles", "SELECT"),
        ("public", "analyzable_articles", "INSERT"),
        ("public", "incomplete_articles", "SELECT"),
        ("public", "incomplete_articles", "INSERT"),
        ("public", "incomplete_articles", "UPDATE"),
        ("public", "incomplete_articles", "DELETE"),
        ("public", "pipeline_events", "INSERT"),
        ("public", "outbox_events", "INSERT"),
    }
    async with system_database.connect("vector_collect") as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {(schema, table) for schema, table, _ in expected} <= tables
    assert actual == expected


async def test_collect_has_only_collection_column_permissions(system_database):
    """収集ロールは保存先の許可列とRETURNING用の列だけを扱える。"""
    async with system_database.connect("vector_collect") as connection:
        columns = await read_table_columns(connection)
        actual = await read_column_permissions(connection)

    expected = {
        ("public", "news_sources", "SELECT"): columns[("public", "news_sources")],
        ("public", "analyzable_articles", "SELECT"): columns[
            ("public", "analyzable_articles")
        ],
        ("public", "analyzable_articles", "INSERT"): columns[
            ("public", "analyzable_articles")
        ],
        ("public", "incomplete_articles", "SELECT"): columns[
            ("public", "incomplete_articles")
        ],
        ("public", "incomplete_articles", "INSERT"): columns[
            ("public", "incomplete_articles")
        ],
        ("public", "incomplete_articles", "UPDATE"): columns[
            ("public", "incomplete_articles")
        ],
        ("public", "pipeline_events", "INSERT"): columns[("public", "pipeline_events")],
        ("public", "pipeline_events", "SELECT"): {"id", "occurred_at"},
        ("public", "outbox_events", "INSERT"): columns[("public", "outbox_events")],
        ("public", "outbox_events", "SELECT"): {
            "event_id",
            "schema_version",
            "occurred_at",
            "next_attempt_at",
            "attempt_count",
        },
    }
    assert actual == expected


async def test_collect_has_usage_only_on_collection_sequences(system_database):
    """収集ロールの採番権限は記事・未完成記事・監査のsequenceだけに限る。"""
    allowed_tables = {"analyzable_articles", "incomplete_articles", "pipeline_events"}
    async with system_database.connect("vector_collect") as connection:
        sequences = await read_sequence_owners(connection)
        actual = await read_sequence_permissions(connection)

    assert allowed_tables <= {
        table for (schema, _), table in sequences.items() if schema == "public"
    }
    expected = {
        (schema, sequence, "USAGE")
        for (schema, sequence), table in sequences.items()
        if schema == "public" and table in allowed_tables
    }
    assert actual == expected


async def test_collect_can_commit_article_and_events_with_returning(system_database):
    """収集ロールで記事・監査・Outboxを追加し、採番と既定値を取得する。"""
    async with system_database.connect("vector_collect") as connection:
        source_id = await connection.fetchval(
            "SELECT id FROM news_sources ORDER BY id LIMIT 1"
        )
        assert source_id is not None

        async with connection.transaction():
            article_id = await connection.fetchval(
                """
                INSERT INTO analyzable_articles
                    (source_id, source_url, original_title,
                     original_content, published_at)
                VALUES ($1, 'https://example.com/permission-article',
                        'title', 'content', now())
                RETURNING id
                """,
                source_id,
            )
            article_title = await connection.fetchval(
                "SELECT original_title FROM analyzable_articles WHERE id=$1",
                article_id,
            )
            event = await connection.fetchrow(
                """
                INSERT INTO pipeline_events
                    (stage, event_type, outcome_code, article_id)
                VALUES ('acquisition', 'succeeded', 'permission_test', $1)
                RETURNING id, occurred_at
                """,
                article_id,
            )
            outbox = await connection.fetchrow(
                """
                INSERT INTO outbox_events (event_type, payload)
                VALUES ('article.acquired', '{}')
                RETURNING event_id, schema_version, occurred_at,
                          next_attempt_at, attempt_count
                """
            )

    assert article_title == "title"
    assert event["id"] is not None
    assert event["occurred_at"] is not None
    assert outbox["event_id"] is not None
    assert outbox["occurred_at"] is not None
    assert outbox["next_attempt_at"] is not None
    assert outbox["schema_version"] == 1
    assert outbox["attempt_count"] == 0

    async with system_database.connect("vector_app") as connection:
        saved_article_id = await connection.fetchval(
            "SELECT article_id FROM pipeline_events WHERE id=$1", event["id"]
        )
        saved_event_type = await connection.fetchval(
            "SELECT event_type FROM outbox_events WHERE event_id=$1",
            outbox["event_id"],
        )

    assert saved_article_id == article_id
    assert saved_event_type == "article.acquired"


async def test_collect_can_crud_incomplete_article(system_database):
    """未完成記事の採番と読取・更新・削除を収集ロールで実行する。"""
    async with system_database.connect("vector_collect") as connection:
        source = await connection.fetchrow(
            "SELECT id, name FROM news_sources ORDER BY id LIMIT 1"
        )
        assert source is not None
        article_id = await connection.fetchval(
            """
            INSERT INTO incomplete_articles
                (url, source_id, source_name, status, observed_article, ready_at)
            VALUES ('https://example.com/permission-incomplete',
                    $1, $2, 'open', '{}', now())
            RETURNING id
            """,
            source["id"],
            source["name"],
        )
        initial_status = await connection.fetchval(
            "SELECT status FROM incomplete_articles WHERE id=$1", article_id
        )
        assert initial_status == "open"

        update_result = await connection.execute(
            "UPDATE incomplete_articles SET status='closed', ready_at=NULL WHERE id=$1",
            article_id,
        )
        updated_status = await connection.fetchval(
            "SELECT status FROM incomplete_articles WHERE id=$1", article_id
        )
        assert update_result == "UPDATE 1"
        assert updated_status == "closed"

        delete_result = await connection.execute(
            "DELETE FROM incomplete_articles WHERE id=$1", article_id
        )
        exists_after_delete = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM incomplete_articles WHERE id=$1)", article_id
        )
        assert delete_result == "DELETE 1"
        assert exists_after_delete is False


async def test_collect_cannot_read_auth_user(system_database):
    """収集ロールは認証ユーザーを参照できない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute('SELECT id FROM auth."user"')
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_read_curations(system_database):
    """収集ロールは後段の整形結果を参照できない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("SELECT * FROM public.article_curations")
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_read_audit_payload(system_database):
    """収集ロールは監査本文を参照できない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("SELECT payload FROM public.pipeline_events")
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_read_outbox_payload(system_database):
    """収集ロールはOutbox本文を参照できない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("SELECT payload FROM public.outbox_events")
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_insert_source(system_database):
    """収集ロールにニュースソースの追加を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(
                "INSERT INTO public.news_sources (name) VALUES ('permission-test')"
            )
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_update_source(system_database):
    """収集ロールにニュースソースの変更を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(
                "UPDATE public.news_sources SET name=name WHERE false"
            )
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_delete_source(system_database):
    """収集ロールにニュースソースの削除を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("DELETE FROM public.news_sources WHERE false")
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_update_article(system_database):
    """収集ロールに保存済み記事の変更を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(
                "UPDATE public.analyzable_articles SET original_title=original_title "
                "WHERE false"
            )
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_delete_article(system_database):
    """収集ロールに保存済み記事の削除を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(
                "DELETE FROM public.analyzable_articles WHERE false"
            )
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_update_audit(system_database):
    """収集ロールに監査本文の変更を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(
                "UPDATE public.pipeline_events SET payload=payload WHERE false"
            )
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_delete_audit(system_database):
    """収集ロールに監査記録の削除を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("DELETE FROM public.pipeline_events WHERE false")
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_mark_outbox_published(system_database):
    """収集ロールにOutbox配信状態の変更を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(
                "UPDATE public.outbox_events SET published_at=now() WHERE false"
            )
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_delete_outbox_event(system_database):
    """収集ロールにOutboxイベントの削除を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("DELETE FROM public.outbox_events WHERE false")
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_truncate_audit(system_database):
    """収集ロールに監査記録の全件消去を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("TRUNCATE public.pipeline_events")
        assert failure.value.sqlstate == "42501"


async def test_collect_cannot_truncate_outbox(system_database):
    """収集ロールにOutboxの全件消去を許可しない。"""
    async with system_database.connect("vector_collect") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("TRUNCATE public.outbox_events")
        assert failure.value.sqlstate == "42501"
