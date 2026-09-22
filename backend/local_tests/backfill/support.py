"""保存済み事実の準備と実接続終了の確認を行う。"""

# ruff: noqa: S101 — テスト専用の検証ヘルパー。

import asyncio
from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
CREATED_AT = NOW - timedelta(hours=1)
EXTRACTED_AT = CREATED_AT + timedelta(minutes=1)
ANALYZED_AT = CREATED_AT + timedelta(minutes=2)


async def seed_article(database, url):
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, "
            "published_at, created_at) "
            "SELECT id, $1, 'title', 'content', $2, $2 FROM news_sources "
            "ORDER BY id LIMIT 1 RETURNING id",
            url,
            CREATED_AT,
        )


async def seed_incomplete_article(database, url):
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO incomplete_articles "
            "(url, source_id, source_name, status, observed_article, "
            "ready_at, created_at) "
            "SELECT $1, id, name, 'open', '{}'::jsonb, $2, $2 FROM news_sources "
            "ORDER BY id LIMIT 1 RETURNING id",
            url,
            CREATED_AT,
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


async def assert_connections_closed(database, scope):
    """実disposeの完了と、別接続から使用済みDB接続の消滅を確認する。"""
    assert scope.disposed
    assert scope.order == ["engine", "rds"]
    async with database.connect("vector_app") as connection:
        deadline = asyncio.get_running_loop().time() + 2
        while await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid=ANY($1::integer[])",
            list(scope.pids),
        ):
            assert asyncio.get_running_loop().time() < deadline, "DB接続が残っている"
            await asyncio.sleep(0.02)
