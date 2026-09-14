# ruff: noqa: S101
"""補完テストの記事準備と、別接続での確定結果の観測。"""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from importlib import import_module

import httpx

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.source_name import SourceName

PUBLISHED_AT = datetime(2026, 9, 13, tzinfo=UTC)


def consumer_contract():
    """fixtureが補完用の設定を用意した後にConsumerを読み込む。"""
    return import_module("app.collection.article_completion.consumer")


def decision_contract():
    return import_module(
        "app.collection.article_completion.consumer_failure_classification"
    )


def article_response(marker="Target discovery"):
    content = escape(
        f"{marker}. Researchers published new measurements from their orbital "
        "instrument today. The experiment recorded changes in atmospheric density "
        "during three separate observation periods. These measurements will help "
        "the team evaluate its next generation of sensors and plan further studies."
    )
    return httpx.Response(
        200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        text=f"<html><head><title>HTML title</title></head><body>"
        f"<article><h1>HTML title</h1><p>{content}</p></article></body></html>",
    )


@dataclass(frozen=True)
class PendingArticle:
    id: int
    source_id: int
    url: str
    title: str


async def seed_pending(database, url, *, status="open", title="Observed target title"):
    observed = ObservedArticle.build(
        source_name=SourceName("VentureBeat"),
        source_url=CanonicalArticleUrl(url),
        title=title,
        body=None,
        published_at=PublishedAt(PUBLISHED_AT),
        origin=ObservedOrigin.feed,
    )
    async with database.connect("vector") as db:
        source_id = await db.fetchval(
            "SELECT id FROM news_sources WHERE name=$1", "VentureBeat"
        )
        assert source_id is not None
        row_id = await db.fetchval(
            "INSERT INTO incomplete_articles "
            "(url, source_id, source_name, status, observed_article, ready_at, "
            "leased_until, attempt_count) VALUES "
            "($1, $2, 'VentureBeat', $3::varchar, $4::jsonb, now(), "
            "CASE WHEN $3::varchar='running' THEN now()+interval '5 minutes' END, 0) "
            "RETURNING id",
            url,
            source_id,
            status,
            observed.model_dump_json(by_alias=True),
        )
    return PendingArticle(row_id, source_id, url, title)


async def seed_completed(database, target):
    async with database.connect("vector") as db:
        return await db.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, published_at) "
            "VALUES ($1, $2, 'Existing title', 'Existing content', $3) RETURNING id",
            target.source_id,
            target.url,
            PUBLISHED_AT,
        )


async def delete_pending(database, pending_article):
    async with database.connect("vector") as db:
        await db.execute(
            "DELETE FROM incomplete_articles WHERE id=$1", pending_article.id
        )


async def has_active_collection_transaction(database):
    async with database.connect("vector") as db:
        return await db.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
            "WHERE datname=current_database() AND usename='vector_collect' "
            "AND xact_start IS NOT NULL)"
        )


@dataclass(frozen=True)
class StoredCompletion:
    pending: dict | None
    articles: list[dict]
    audits: list[dict]
    outbox: list[dict]

    @property
    def successes(self):
        return [row for row in self.audits if row["event_type"] == "succeeded"]


def decoded(rows):
    values = [dict(row) for row in rows]
    for row in values:
        if isinstance(row.get("payload"), str):
            row["payload"] = json.loads(row["payload"])
    return values


async def stored_completion(database, target):
    async with database.connect("vector_app") as db:
        pending = await db.fetchrow(
            "SELECT id, status, observed_article, ready_at, "
            "leased_until, attempt_count "
            "FROM incomplete_articles WHERE id=$1",
            target.id,
        )
        articles = await db.fetch(
            "SELECT id, source_id, original_title, original_content, published_at "
            "FROM analyzable_articles WHERE source_url=$1 ORDER BY id",
            target.url,
        )
        audits = await db.fetch(
            "SELECT id, article_id, event_type, outcome_code, payload "
            "FROM pipeline_events WHERE stage='completion' "
            "AND (payload->>'canonical_url'=$1 "
            "OR (payload->>'incomplete_article_id')::bigint=$2 "
            "OR article_id IN (SELECT id FROM analyzable_articles "
            "WHERE source_url=$1)) "
            "ORDER BY id",
            target.url,
            target.id,
        )
        outbox = await db.fetch(
            "SELECT event_id, event_type, schema_version, payload FROM outbox_events "
            "WHERE event_type='article.analyzable_created' "
            "AND (payload->>'analyzable_article_id')::bigint IN "
            "(SELECT id FROM analyzable_articles WHERE source_url=$1) "
            "ORDER BY event_id",
            target.url,
        )
    return StoredCompletion(
        dict(pending) if pending is not None else None,
        decoded(articles),
        decoded(audits),
        decoded(outbox),
    )


async def wait_until_blocked(database, blocker_pid, tasks):
    async with database.connect("vector") as db:
        async with asyncio.timeout(5):
            while True:
                assert all(not task.done() for task in tasks), (
                    "競合待機前に処理が終了した"
                )
                if await db.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname=current_database() AND wait_event_type='Lock' "
                    "AND $1::integer=ANY(pg_blocking_pids(pid)))",
                    blocker_pid,
                ):
                    return
                await asyncio.sleep(0.02)
