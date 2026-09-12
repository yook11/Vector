# ruff: noqa: S101
"""Assessmentの記事準備と、実入口・別接続からの保存結果の観測を共有する。"""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

import httpx

from app.analysis.curation.events import ArticleCuratedSignal
from app.outbox.publishing.curated_signal import build_curated_signal_message
from app.outbox.publishing.publisher import EventEnvelope

handler_module = import_module("app.lambda_handlers.assessment.handler")


async def seed_curation(
    database, source_url, *, title="対象タイトル", summary="対象要約"
):
    async with database.connect("vector") as connection:
        article_id = await connection.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, published_at) "
            "SELECT id, $1, 'original title', 'original content', now() "
            "FROM news_sources ORDER BY id LIMIT 1 RETURNING id",
            source_url,
        )
        assert article_id is not None, "記事準備に必要なソースが存在しない"
        curation_id = await connection.fetchval(
            "INSERT INTO article_curations "
            "(analyzable_article_id, translated_title, summary) "
            "VALUES ($1, $2, $3) RETURNING id",
            article_id,
            title,
            summary,
        )
    return ArticleCuratedSignal(
        curation_id=curation_id, analyzable_article_id=article_id
    )


def build_sqs_record(payload):
    message = build_curated_signal_message(
        EventEnvelope(
            event_id=uuid4(),
            event_type=payload.EVENT_TYPE,
            schema_version=payload.SCHEMA_VERSION,
            occurred_at=datetime.now(UTC),
            payload=payload.model_dump(),
        )
    )
    return {"messageId": str(payload.curation_id), "body": message.body}


async def invoke_sqs_record(record):
    return await asyncio.to_thread(handler_module.handler, {"Records": [record]}, None)


async def invoke_event(payload):
    return await invoke_sqs_record(build_sqs_record(payload))


def deepseek_reply(*, category="ai", investor_take="投資判断", key_points=()):
    """実SDKが読み取るHTTP応答に、テストで指定した判定内容を載せる。"""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-test",
                                "type": "function",
                                "function": {
                                    "name": "assess_article",
                                    "arguments": json.dumps(
                                        {
                                            "category": category,
                                            "investor_take": investor_take,
                                            "key_points": list(key_points),
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
        },
    )


@dataclass(frozen=True)
class StoredAssessment:
    in_scope: list[dict]
    out_of_scope: list[dict]
    audits: list[dict]
    outbox: list[dict]


def _decode_rows(rows):
    decoded = [dict(row) for row in rows]
    for row in decoded:
        for name in ("key_points", "payload"):
            if name in row and isinstance(row[name], str):
                row[name] = json.loads(row[name])
    return decoded


async def fetch_stored_assessment(database, curation_id):
    """実アプリ権限の別接続から、対象結果・監査・Outboxの確定済み内容を取得する。"""
    async with database.connect("vector_app") as connection:
        in_scope = await connection.fetch(
            "SELECT a.id, a.translated_title, a.summary, a.investor_take, "
            "a.key_points, c.slug AS category "
            "FROM analyzed_articles a JOIN categories c ON c.id=a.category_id "
            "WHERE a.curation_id=$1 ORDER BY a.id",
            curation_id,
        )
        out_of_scope = await connection.fetch(
            "SELECT id, translated_title, summary, investor_take, key_points "
            "FROM out_of_scope_articles WHERE curation_id=$1 ORDER BY id",
            curation_id,
        )
        audits = await connection.fetch(
            "SELECT id, article_id, event_type, outcome_code, payload "
            "FROM pipeline_events WHERE stage='assessment' "
            "AND (payload->>'curation_id')::integer=$1 ORDER BY id",
            curation_id,
        )
        outbox = await connection.fetch(
            "SELECT event_id, event_type, schema_version, occurred_at, payload "
            "FROM outbox_events WHERE event_type='article.assessed_in_scope' "
            "AND (payload->>'curation_id')::integer=$1 ORDER BY event_id",
            curation_id,
        )
    return StoredAssessment(
        *map(_decode_rows, (in_scope, out_of_scope, audits, outbox))
    )


async def wait_for_blocked_connection(database, blocker_pid, invocations):
    """指定の接続に遮られた実AssessmentのSQLがロック待ちになるまで待つ。"""
    deadline = asyncio.get_running_loop().time() + 3
    async with database.connect("vector_app") as connection:
        while True:
            assert all(not task.done() for task in invocations), (
                "ロック待ちになる前に処理が終了した"
            )
            pid = await connection.fetchval(
                "SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
                "AND application_name='vector-assessment-consumer' "
                "AND wait_event_type='Lock' AND $1::integer=ANY(pg_blocking_pids(pid))",
                blocker_pid,
            )
            if pid is not None:
                return pid
            assert asyncio.get_running_loop().time() < deadline, (
                "保存がロック待ちにならなかった"
            )
            await asyncio.sleep(0.02)
