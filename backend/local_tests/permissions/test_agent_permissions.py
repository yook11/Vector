"""agentロールの権限が許可一覧と一致し、動作試験で検出できない過剰が無いことを確認する。"""

import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_owners,
    read_sequence_permissions,
    read_table_columns,
    read_table_permissions,
    read_tables,
)

ROLE = "vector_agent"
# 回答の根拠に使える公開ニュース・成果物と、runの実行で読むagentの表。
READ_TABLES = (
    "analyzable_articles",
    "article_curations",
    "analyzed_articles",
    "categories",
    "news_sources",
    "weekly_briefings",
    "trends_snapshots",
    "agent_threads",
    "agent_messages",
    "agent_message_sources",
    "agent_runs",
    "agent_user_daily_quotas",
    "query_embedding_cache",
)
# 回答・出典・埋め込みを追加だけで保存する表。
INSERT_TABLES = ("agent_messages", "agent_message_sources", "query_embedding_cache")
SEQUENCE_TABLES = {"agent_message_sources", "query_embedding_cache"}
pytestmark = pytest.mark.asyncio


async def test_agent_has_only_listed_table_permissions(system_database):
    """表単位の権限は読む表のSELECT、runの更新、回答・出典・埋め込みの追加だけに限る。"""
    expected = {
        *(("public", table, "SELECT") for table in READ_TABLES),
        *(("public", table, "INSERT") for table in INSERT_TABLES),
        ("public", "agent_runs", "UPDATE"),
    }
    async with system_database.connect(ROLE) as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {(schema, table) for schema, table, _ in expected} <= tables
    assert actual == expected


async def test_agent_has_only_listed_column_permissions(system_database):
    """スレッドと利用枠の更新は、完了と利用枠の返却で書く列だけに限る。"""
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
            for table in INSERT_TABLES
        },
        ("public", "agent_runs", "UPDATE"): columns[("public", "agent_runs")],
        ("public", "agent_threads", "UPDATE"): {"updated_at", "research_handoff"},
        ("public", "agent_user_daily_quotas", "UPDATE"): {"used_count"},
    }
    assert actual == expected


async def test_agent_has_usage_only_on_source_and_embedding_sequences(
    system_database,
):
    """採番権限は出典と埋め込みを追加する表のsequenceだけに限る。"""
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
