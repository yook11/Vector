"""Curationの記事準備と、実入口・管理接続からの保存結果の観測を共有する。"""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

import httpx

from app.collection.events import AnalyzableArticleCreated
from app.outbox.publishing.analyzable_created import build_analyzable_created_message
from app.outbox.publishing.publisher import EventEnvelope

handler_module = import_module("app.lambda_handlers.curation.handler")


async def seed_article(database, source_url, *, title, content):
    async with database.connect("vector") as connection:
        article_id = await connection.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, published_at) "
            "SELECT id, $1, $2, $3, now() "
            "FROM news_sources ORDER BY id LIMIT 1 RETURNING id",
            source_url,
            title,
            content,
        )
    if article_id is None:
        raise RuntimeError("記事の準備に必要なソースが存在しない")
    return AnalyzableArticleCreated(analyzable_article_id=article_id)


def curation_reply(*, relevance, title_ja, summary_ja):
    """実SDKが読み取るHTTP応答に、テストで指定した判定内容を載せる。"""
    text = json.dumps(
        {"relevance": relevance, "title_ja": title_ja, "summary_ja": summary_ja}
    )
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": "STOP",
                }
            ]
        },
    )


def build_sqs_record(payload):
    message = build_analyzable_created_message(
        EventEnvelope(
            event_id=uuid4(),
            event_type=payload.EVENT_TYPE,
            schema_version=payload.SCHEMA_VERSION,
            occurred_at=datetime.now(UTC),
            payload=payload.model_dump(),
        )
    )
    return {"messageId": str(payload.analyzable_article_id), "body": message.body}


async def invoke_event(payload):
    return await asyncio.to_thread(
        handler_module.handler, {"Records": [build_sqs_record(payload)]}, None
    )


@dataclass(frozen=True)
class StoredCuration:
    curations: list[dict]
    noises: list[dict]
    audits: list[dict]
    outbox_payloads: list[dict]


async def fetch_stored_curation(database, analyzable_article_id):
    """管理接続から、対象記事の結果・監査・Outboxの確定済み内容を取得する。"""
    async with database.connect("vector") as connection:
        curations = await connection.fetch(
            "SELECT id, translated_title, summary FROM article_curations "
            "WHERE analyzable_article_id=$1 ORDER BY id",
            analyzable_article_id,
        )
        noises = await connection.fetch(
            "SELECT translated_title, summary FROM curation_noises "
            "WHERE analyzable_article_id=$1 ORDER BY id",
            analyzable_article_id,
        )
        audits = await connection.fetch(
            "SELECT event_type, outcome_code FROM pipeline_events "
            "WHERE stage='curation' AND article_id=$1 ORDER BY id",
            analyzable_article_id,
        )
        outbox = await connection.fetch(
            "SELECT payload FROM outbox_events "
            "WHERE event_type='article.curated_signal' "
            "AND (payload->>'analyzable_article_id')::integer=$1 ORDER BY event_id",
            analyzable_article_id,
        )
    return StoredCuration(
        curations=[dict(row) for row in curations],
        noises=[dict(row) for row in noises],
        audits=[dict(row) for row in audits],
        outbox_payloads=[json.loads(row["payload"]) for row in outbox],
    )
