"""Embeddingテストで共有する記事準備と実イベント入口の呼び出し。"""

import asyncio
from datetime import UTC, datetime
from importlib import import_module
from uuid import UUID

from app.analysis.assessment.events import (
    ArticleAssessedInScope,
    ArticleAssessedInScopeEvent,
)
from app.outbox.sqs.message import SqsMessage

_handler_module = import_module("app.lambda_handlers.embedding.handler")


async def seed_article(database, source_url, *, title="title", summary="summary"):
    async with database.connect("vector") as connection:
        article_id = await connection.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, published_at) "
            "SELECT id, $1, $2, 'content', now() "
            "FROM news_sources ORDER BY id LIMIT 1 RETURNING id",
            source_url,
            title,
        )
        if article_id is None:
            raise RuntimeError("記事の準備に必要なソースが存在しない")
        curation_id = await connection.fetchval(
            "INSERT INTO article_curations "
            "(analyzable_article_id, translated_title, summary) "
            "VALUES ($1, $2, $3) RETURNING id",
            article_id,
            title,
            summary,
        )
        analyzed_id = await connection.fetchval(
            "INSERT INTO analyzed_articles "
            "(curation_id, translated_title, summary, investor_take, category_id) "
            "SELECT $1, $2, $3, 'take', id "
            "FROM categories ORDER BY id LIMIT 1 RETURNING id",
            curation_id,
            title,
            summary,
        )
        if analyzed_id is None:
            raise RuntimeError("分析記事の準備に必要なカテゴリが存在しない")
        return ArticleAssessedInScope(
            curation_id=curation_id, analyzed_article_id=analyzed_id
        )


async def fetch_stored_embedding(database, analyzed_article_id):
    """実アプリ権限の別接続から、確定済みのベクトルをテキストで取得する。"""
    async with database.connect("vector_app") as connection:
        return await connection.fetchval(
            "SELECT embedding::text FROM analyzed_articles WHERE id = $1",
            analyzed_article_id,
        )


def build_sqs_record(payload):
    event = ArticleAssessedInScopeEvent(
        event_id=UUID(int=1),
        event_type=payload.EVENT_TYPE,
        schema_version=payload.SCHEMA_VERSION,
        occurred_at=datetime.now(UTC),
        payload=payload,
    )
    return {
        "messageId": str(payload.analyzed_article_id),
        "body": SqsMessage.from_event(event).body,
    }


async def invoke_event(payload):
    return await invoke_sqs_record(build_sqs_record(payload))


async def invoke_sqs_record(record):
    return await asyncio.to_thread(
        _handler_module.handler,
        {"Records": [record]},
        None,
    )
