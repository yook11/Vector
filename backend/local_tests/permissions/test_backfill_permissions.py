"""backfillロールの権限が許可一覧と一致し、フロー試験で検出できない過剰が無いことを確認する。"""

import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_owners,
    read_sequence_permissions,
    read_table_columns,
    read_table_permissions,
    read_tables,
)

ROLE = "vector_backfill"
# 対象抽出で読む記事・分析結果・除外・未完成記事・ニュースソースの表。
READ_TABLES = (
    "analyzable_articles",
    "article_curations",
    "curation_noises",
    "analyzed_articles",
    "out_of_scope_articles",
    "assessment_backfill_exclusions",
    "embedding_backfill_exclusions",
    "incomplete_articles",
    "news_sources",
)
EXCLUSION_TABLES = (
    "assessment_backfill_exclusions",
    "embedding_backfill_exclusions",
)
pytestmark = pytest.mark.asyncio


async def test_backfill_has_only_listed_table_permissions(system_database):
    """表単位の権限は読む表のSELECT、記事の削除、除外と監査の追加だけに限る。"""
    expected = {
        *(("public", table, "SELECT") for table in READ_TABLES),
        ("public", "analyzable_articles", "DELETE"),
        *(("public", table, "INSERT") for table in EXCLUSION_TABLES),
        ("public", "pipeline_events", "INSERT"),
    }
    async with system_database.connect(ROLE) as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {(schema, table) for schema, table, _ in expected} <= tables
    assert actual == expected


async def test_backfill_has_only_listed_column_permissions(system_database):
    """列の更新は行ロック用のidと未完成行の打ち切りに使う列だけに限る。"""
    async with system_database.connect(ROLE) as connection:
        columns = await read_table_columns(connection)
        actual = await read_column_permissions(connection)

    expected = {
        **{
            ("public", table, "SELECT"): columns[("public", table)]
            for table in READ_TABLES
        },
        **{
            ("public", table, "INSERT"): columns[("public", table)]
            for table in EXCLUSION_TABLES
        },
        ("public", "analyzable_articles", "UPDATE"): {"id"},
        ("public", "article_curations", "UPDATE"): {"id"},
        ("public", "analyzed_articles", "UPDATE"): {"id"},
        ("public", "incomplete_articles", "UPDATE"): {
            "status",
            "leased_until",
            "updated_at",
        },
        ("public", "pipeline_events", "INSERT"): columns[("public", "pipeline_events")],
        ("public", "pipeline_events", "SELECT"): {"id", "occurred_at"},
    }
    assert actual == expected


async def test_backfill_has_usage_only_on_audit_sequence(system_database):
    """採番権限は監査を追加するpipeline_eventsのsequenceだけに限る。"""
    async with system_database.connect(ROLE) as connection:
        sequences = await read_sequence_owners(connection)
        actual = await read_sequence_permissions(connection)

    assert "pipeline_events" in {
        table for (schema, _), table in sequences.items() if schema == "public"
    }
    expected = {
        (schema, sequence, "USAGE")
        for (schema, sequence), table in sequences.items()
        if schema == "public" and table == "pipeline_events"
    }
    assert actual == expected
