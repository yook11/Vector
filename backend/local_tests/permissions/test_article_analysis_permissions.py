"""記事分析ロールの許可一覧と、分析結果・監査・Outboxの保護を確認する。"""

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

ROLE = "vector_article_analysis"
pytestmark = pytest.mark.asyncio


async def test_article_analysis_has_only_analysis_table_permissions(system_database):
    """記事分析ロールの表権限は読む表・保存先の許可一覧と完全一致する。"""
    expected = {
        ("public", "analyzable_articles", "SELECT"),
        ("public", "categories", "SELECT"),
        ("public", "article_curations", "SELECT"),
        ("public", "article_curations", "INSERT"),
        ("public", "curation_noises", "SELECT"),
        ("public", "curation_noises", "INSERT"),
        ("public", "analyzed_articles", "SELECT"),
        ("public", "analyzed_articles", "INSERT"),
        ("public", "out_of_scope_articles", "SELECT"),
        ("public", "out_of_scope_articles", "INSERT"),
        ("public", "pipeline_events", "INSERT"),
        ("public", "outbox_events", "INSERT"),
    }
    async with system_database.connect(ROLE) as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {(schema, table) for schema, table, _ in expected} <= tables
    assert actual == expected


async def test_article_analysis_has_only_analysis_column_permissions(system_database):
    """表単位の許可に加え、embedding列の更新とRETURNING用の列だけを扱える。"""
    async with system_database.connect(ROLE) as connection:
        columns = await read_table_columns(connection)
        actual = await read_column_permissions(connection)

    expected = {
        ("public", "analyzable_articles", "SELECT"): columns[
            ("public", "analyzable_articles")
        ],
        ("public", "categories", "SELECT"): columns[("public", "categories")],
        ("public", "article_curations", "SELECT"): columns[
            ("public", "article_curations")
        ],
        ("public", "article_curations", "INSERT"): columns[
            ("public", "article_curations")
        ],
        ("public", "curation_noises", "SELECT"): columns[("public", "curation_noises")],
        ("public", "curation_noises", "INSERT"): columns[("public", "curation_noises")],
        ("public", "analyzed_articles", "SELECT"): columns[
            ("public", "analyzed_articles")
        ],
        ("public", "analyzed_articles", "INSERT"): columns[
            ("public", "analyzed_articles")
        ],
        ("public", "analyzed_articles", "UPDATE"): {"embedding"},
        ("public", "out_of_scope_articles", "SELECT"): columns[
            ("public", "out_of_scope_articles")
        ],
        ("public", "out_of_scope_articles", "INSERT"): columns[
            ("public", "out_of_scope_articles")
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


async def test_article_analysis_has_usage_only_on_saved_table_sequences(
    system_database,
):
    """採番権限は分析が行を追加する表のsequenceだけに限る。"""
    allowed_tables = {
        "article_curations",
        "curation_noises",
        "analyzed_articles",
        "out_of_scope_articles",
        "pipeline_events",
    }
    async with system_database.connect(ROLE) as connection:
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


async def _assert_denied(system_database, statement):
    async with system_database.connect(ROLE) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(statement)
        assert failure.value.sqlstate == "42501"


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM public.article_curations WHERE false",
        "DELETE FROM public.curation_noises WHERE false",
        "DELETE FROM public.analyzed_articles WHERE false",
        "DELETE FROM public.out_of_scope_articles WHERE false",
        "TRUNCATE public.analyzed_articles",
    ],
)
async def test_article_analysis_cannot_delete_results(system_database, statement):
    """保存済みの分析結果を削除できない。"""
    await _assert_denied(system_database, statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE public.analyzed_articles SET summary=summary WHERE false",
        "UPDATE public.article_curations SET summary=summary WHERE false",
        "UPDATE public.curation_noises SET summary=summary WHERE false",
        "UPDATE public.out_of_scope_articles SET summary=summary WHERE false",
    ],
)
async def test_article_analysis_cannot_rewrite_saved_results(
    system_database, statement
):
    """embedding以外の保存済み結果を書き換えられない。"""
    await _assert_denied(system_database, statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE public.analyzable_articles "
        "SET original_title=original_title WHERE false",
        "UPDATE public.categories SET name=name WHERE false",
    ],
)
async def test_article_analysis_cannot_change_source_articles_or_categories(
    system_database, statement
):
    """分析の入力となる元記事とカテゴリを変更できない。"""
    await _assert_denied(system_database, statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT payload FROM public.pipeline_events",
        "SELECT payload FROM public.outbox_events",
    ],
)
async def test_article_analysis_cannot_read_audit_or_outbox_payload(
    system_database, statement
):
    """全段が書く監査とOutboxの本文を参照できない。"""
    await _assert_denied(system_database, statement)


async def test_article_analysis_cannot_change_outbox_delivery_state(system_database):
    """配送状態はRelayの担当で、分析からは変更できない。"""
    await _assert_denied(
        system_database,
        "UPDATE public.outbox_events SET published_at=now() WHERE false",
    )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT id FROM public.news_sources",
        'SELECT id FROM auth."user"',
    ],
)
async def test_article_analysis_cannot_access_other_process_tables(
    system_database, statement
):
    """分析で使わない他の処理の表と認証情報に到達できない。"""
    await _assert_denied(system_database, statement)
