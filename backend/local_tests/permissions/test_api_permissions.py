"""APIロールの権限が許可一覧と一致し、動作試験で検出できない過剰が無いことを確認する。"""

import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_owners,
    read_sequence_permissions,
    read_table_columns,
    read_table_permissions,
    read_tables,
)

ROLE = "vector_api"
# ニュース・成果物の表示と健全性画面で読み、書き込まない表。
READ_ONLY_TABLES = (
    "analyzable_articles",
    "article_curations",
    "analyzed_articles",
    "categories",
    "weekly_briefings",
    "trends_snapshots",
    "incomplete_articles",
    "curation_noises",
    "out_of_scope_articles",
    "assessment_backfill_exclusions",
    "embedding_backfill_exclusions",
    "agent_message_sources",
)
# 利用者・運用者の操作で行を追加する表。
INSERT_TABLES = (
    "news_sources",
    "watchlist_entries",
    "agent_threads",
    "agent_messages",
    "agent_runs",
    "agent_user_daily_quotas",
)
DELETE_TABLES = ("news_sources", "watchlist_entries", "agent_threads")
pytestmark = pytest.mark.asyncio


async def test_api_has_only_listed_table_permissions(system_database):
    """表単位の権限は読む表のSELECTと、利用者・運用者の操作での追加・削除だけに限る。"""
    expected = {
        *(("public", table, "SELECT") for table in READ_ONLY_TABLES + INSERT_TABLES),
        *(("public", table, "INSERT") for table in INSERT_TABLES),
        *(("public", table, "DELETE") for table in DELETE_TABLES),
    }
    async with system_database.connect(ROLE) as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {(schema, table) for schema, table, _ in expected} <= tables
    assert actual == expected


async def test_api_has_only_listed_column_permissions(system_database):
    """列の更新は操作で変わる列だけに限り、監査は健全性画面が集計する列だけを読む。"""
    async with system_database.connect(ROLE) as connection:
        columns = await read_table_columns(connection)
        actual = await read_column_permissions(connection)

    expected = {
        **{
            ("public", table, "SELECT"): columns[("public", table)]
            for table in READ_ONLY_TABLES + INSERT_TABLES
        },
        **{
            ("public", table, "INSERT"): columns[("public", table)]
            for table in INSERT_TABLES
        },
        ("public", "news_sources", "UPDATE"): {"is_active", "updated_at"},
        ("public", "agent_threads", "UPDATE"): {"updated_at"},
        ("public", "agent_runs", "UPDATE"): {"status", "error_code"},
        ("public", "agent_user_daily_quotas", "UPDATE"): {"used_count"},
        ("public", "pipeline_events", "SELECT"): {
            "stage",
            "event_type",
            "outcome_code",
            "source_id",
            "occurred_at",
        },
    }
    assert actual == expected


async def test_api_has_usage_only_on_news_source_sequence(system_database):
    """採番権限はソースを登録するnews_sourcesのsequenceだけに限る。"""
    async with system_database.connect(ROLE) as connection:
        sequences = await read_sequence_owners(connection)
        actual = await read_sequence_permissions(connection)

    assert "news_sources" in {
        table for (schema, _), table in sequences.items() if schema == "public"
    }
    expected = {
        (schema, sequence, "USAGE")
        for (schema, sequence), table in sequences.items()
        if schema == "public" and table == "news_sources"
    }
    assert actual == expected
