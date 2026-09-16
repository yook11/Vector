"""Outbox配信ロールの許可一覧と禁止操作を確認する。"""

import asyncpg
import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_permissions,
    read_table_permissions,
)

pytestmark = pytest.mark.asyncio


async def test_relay_has_only_delivery_column_permissions(system_database):
    """Relayの列権限は配信に必要な参照・更新の一覧と完全一致する。"""
    expected = {
        ("public", "outbox_events", "SELECT"): {
            "event_id",
            "event_type",
            "schema_version",
            "payload",
            "occurred_at",
            "published_at",
            "next_attempt_at",
            "attempt_count",
            "lease_token",
            "leased_until",
            "delivery_stopped_at",
        },
        ("public", "outbox_events", "UPDATE"): {
            "lease_token",
            "leased_until",
            "attempt_count",
            "published_at",
            "next_attempt_at",
            "delivery_stopped_at",
            "delivery_stop_reason",
        },
    }

    async with system_database.connect("vector_outbox_relay") as connection:
        actual = await read_column_permissions(connection)

    assert actual == expected


async def test_relay_has_no_table_wide_permissions(system_database):
    """Relayには列単位の許可を越える表全体の操作権限がない。"""
    async with system_database.connect("vector_outbox_relay") as connection:
        actual = await read_table_permissions(connection)

    assert actual == set()


async def test_relay_has_no_sequence_permissions(system_database):
    """Relayには採番やsequenceの操作権限がない。"""
    async with system_database.connect("vector_outbox_relay") as connection:
        actual = await read_sequence_permissions(connection)

    assert actual == set()


async def _assert_relay_denied(system_database, statement):
    async with system_database.connect("vector_outbox_relay") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(statement)
        assert failure.value.sqlstate == "42501"


async def test_relay_cannot_change_event_payload(system_database):
    """配信担当者はイベント本文を書き換えられない。"""
    await _assert_relay_denied(
        system_database, "UPDATE public.outbox_events SET payload='{}' WHERE false"
    )


async def test_relay_cannot_change_event_type(system_database):
    """配信担当者はイベントの種類を書き換えられない。"""
    await _assert_relay_denied(
        system_database,
        "UPDATE public.outbox_events SET event_type='changed' WHERE false",
    )


async def test_relay_cannot_read_delivery_stop_reason(system_database):
    """停止理由は更新用の列として扱い参照を許可しない。"""
    await _assert_relay_denied(
        system_database, "SELECT delivery_stop_reason FROM public.outbox_events"
    )


async def test_relay_cannot_insert_event(system_database):
    """配信担当者に新しいイベントの作成を許可しない。"""
    await _assert_relay_denied(
        system_database,
        "INSERT INTO public.outbox_events (event_type,payload) "
        "VALUES ('test.outbox','{}')",
    )


async def test_relay_cannot_delete_event(system_database):
    """配信担当者にイベントの削除を許可しない。"""
    await _assert_relay_denied(
        system_database, "DELETE FROM public.outbox_events WHERE false"
    )


async def test_relay_cannot_truncate_outbox(system_database):
    """配信担当者にOutboxの全件消去を許可しない。"""
    await _assert_relay_denied(system_database, "TRUNCATE public.outbox_events")


async def test_relay_cannot_read_articles(system_database):
    """イベントの本文を参照できても記事テーブルにはアクセスできない。"""
    await _assert_relay_denied(
        system_database, "SELECT id FROM public.analyzed_articles"
    )


async def test_relay_cannot_read_auth_users(system_database):
    """配信担当者に認証ユーザーの参照を許可しない。"""
    await _assert_relay_denied(system_database, 'SELECT id FROM auth."user"')


async def test_relay_cannot_read_migration_version(system_database):
    """配信担当者にmigration管理表の参照を許可しない。"""
    await _assert_relay_denied(
        system_database, "SELECT version_num FROM public.alembic_version"
    )


async def test_relay_cannot_create_public_table(system_database):
    """配信担当者にアプリschemaへのDDLを許可しない。"""
    await _assert_relay_denied(
        system_database, "CREATE TABLE public.relay_forbidden (id integer)"
    )


@pytest.mark.parametrize(
    "statement",
    [
        "SET ROLE vector",
        "SET ROLE vector_app",
        "SET ROLE vector_auth",
        "SET ROLE vector_collect",
    ],
)
async def test_relay_cannot_assume_another_role(system_database, statement):
    """配信担当者は別の認証主体へ切り替えて権限を得られない。"""
    await _assert_relay_denied(system_database, statement)


async def test_relay_does_not_own_database_objects(system_database):
    """配信ロールが所有権によってGRANTを越える権限を持たない。"""
    async with system_database.connect("vector_outbox_relay") as connection:
        assert not await connection.fetchval(
            "SELECT EXISTS (SELECT FROM pg_class WHERE "
            "relowner=current_user::regrole) OR EXISTS (SELECT FROM pg_namespace "
            "WHERE nspowner=current_user::regrole) OR EXISTS (SELECT FROM "
            "pg_database WHERE datdba=current_user::regrole)"
        )
