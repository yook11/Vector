"""Insightsロールの権限が許可一覧と一致し、動作試験で検出できない過剰が無いことを確認する。"""

import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_owners,
    read_sequence_permissions,
    read_table_columns,
    read_table_permissions,
    read_tables,
)

ROLE = "vector_insights"
# トレンドの集計とブリーフィングの入力に読む表。
READ_TABLES = (
    "analyzable_articles",
    "article_curations",
    "analyzed_articles",
    "categories",
)
# 生成済みかを確かめ、追加だけで保存する成果物の表。
OUTPUT_TABLES = ("trends_snapshots", "weekly_briefings")
SEQUENCE_TABLES = {"weekly_briefings", "pipeline_events"}
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM public.analyzable_articles",
        "SELECT * FROM public.article_curations",
    ],
)
async def test_insights_can_read_all_columns_of_article_tables(system_database, query):
    """記事2表の全列をInsightsロール自身で参照できる。"""
    async with system_database.connect(ROLE) as connection:
        assert await connection.fetch(query) == []


async def test_insights_has_only_listed_table_permissions(system_database):
    """表単位の権限は分析結果の参照、成果物の参照と追加、監査の追加だけに限る。"""
    expected = {
        *(("public", table, "SELECT") for table in READ_TABLES),
        *(("public", table, "SELECT") for table in OUTPUT_TABLES),
        *(("public", table, "INSERT") for table in OUTPUT_TABLES),
        ("public", "pipeline_events", "INSERT"),
    }
    async with system_database.connect(ROLE) as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {(schema, table) for schema, table, _ in expected} <= tables
    assert actual == expected


async def test_insights_has_only_listed_column_permissions(system_database):
    """列の権限は表単位の付与に含まれる列と、監査のRETURNINGで受け取る2列だけに限る。"""
    async with system_database.connect(ROLE) as connection:
        columns = await read_table_columns(connection)
        actual = await read_column_permissions(connection)

    expected = {
        **{
            ("public", table, "SELECT"): columns[("public", table)]
            for table in (*READ_TABLES, *OUTPUT_TABLES)
        },
        **{
            ("public", table, "INSERT"): columns[("public", table)]
            for table in OUTPUT_TABLES
        },
        ("public", "pipeline_events", "INSERT"): columns[("public", "pipeline_events")],
        ("public", "pipeline_events", "SELECT"): {"id", "occurred_at"},
    }
    assert actual == expected


async def test_insights_has_usage_only_on_briefing_and_audit_sequences(
    system_database,
):
    """採番権限はブリーフィングと監査を追加する表のsequenceだけに限る。"""
    async with system_database.connect(ROLE) as connection:
        sequences = await read_sequence_owners(connection)
        actual = await read_sequence_permissions(connection)

    assert SEQUENCE_TABLES <= {
        table for (schema, _), table in sequences.items() if schema == "public"
    }
    expected = {
        (schema, sequence, "USAGE")
        for (schema, sequence), table in sequences.items()
        if schema == "public" and table in SEQUENCE_TABLES
    }
    assert actual == expected
