"""保存済み事実の準備、保存結果の読み取り、実接続終了の確認を行う。"""

# ruff: noqa: S101 — テスト専用の検証ヘルパー。

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
CREATED_AT = NOW - timedelta(hours=1)
# 救済の期間（7日）を超え、期限切れとして整理される作成時刻。
AGED_CREATED_AT = NOW - timedelta(days=8)
EXTRACTED_AT = CREATED_AT + timedelta(minutes=1)
ANALYZED_AT = CREATED_AT + timedelta(minutes=2)


class AuditEvent(NamedTuple):
    stage: str
    event_type: str
    outcome_code: str
    article_id: int | None
    payload: dict[str, Any]


async def seed_article(database, url, created_at=CREATED_AT):
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, "
            "published_at, created_at) "
            "SELECT id, $1, 'title', 'content', $2, $2 FROM news_sources "
            "ORDER BY id LIMIT 1 RETURNING id",
            url,
            created_at,
        )


async def seed_incomplete_article(database, url, created_at=CREATED_AT):
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO incomplete_articles "
            "(url, source_id, source_name, status, observed_article, "
            "ready_at, created_at) "
            "SELECT $1, id, name, 'open', '{}'::jsonb, $2, $2 FROM news_sources "
            "ORDER BY id LIMIT 1 RETURNING id",
            url,
            created_at,
        )


async def seed_curation(database, article_id):
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO article_curations "
            "(analyzable_article_id, translated_title, summary, extracted_at) "
            "VALUES ($1, 'title', 'summary', $2) RETURNING id",
            article_id,
            EXTRACTED_AT,
        )


async def seed_analysis(database, curation_id):
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO analyzed_articles "
            "(curation_id, translated_title, summary, investor_take, "
            "category_id, analyzed_at) "
            "SELECT $1, 'title', 'summary', 'take', id, $2 FROM categories "
            "ORDER BY id LIMIT 1 RETURNING id",
            curation_id,
            ANALYZED_AT,
        )


async def read_audit_events(database):
    """確定した監査を追加順に返す。"""
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT stage, event_type, outcome_code, article_id, payload "
            "FROM pipeline_events ORDER BY id"
        )
    return [
        AuditEvent(
            row["stage"],
            row["event_type"],
            row["outcome_code"],
            row["article_id"],
            json.loads(row["payload"]),
        )
        for row in rows
    ]


async def assert_connections_closed(database, scope):
    """実disposeの完了と、別接続から使用済みDB接続の消滅を確認する。"""
    assert scope.disposed
    assert scope.order == ["engine", "rds"]
    async with database.connect("vector_backfill") as connection:
        deadline = asyncio.get_running_loop().time() + 2
        while await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid=ANY($1::integer[])",
            list(scope.pids),
        ):
            assert asyncio.get_running_loop().time() < deadline, "DB接続が残っている"
            await asyncio.sleep(0.02)
