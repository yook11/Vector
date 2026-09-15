"""取得テストのRSS応答と、確定した保存結果の読み取り。"""

import json
from html import escape

import httpx


def rss_article_response(*, title: str, url: str, body: str | None) -> httpx.Response:
    description = f"<description>{escape(body)}</description>" if body else ""
    return httpx.Response(
        200,
        text=(
            '<rss version="2.0"><channel><title>VentureBeat</title><item>'
            f"<title>{escape(title)}</title><link>{escape(url)}</link>"
            "<pubDate>Sun, 13 Sep 2026 00:00:00 GMT</pubDate>"
            f"{description}</item></channel></rss>"
        ),
    )


async def load_completed_articles(database):
    async with database.connect("vector_app") as reader:
        rows = await reader.fetch(
            "SELECT id, source_id, source_url, original_title, original_content "
            "FROM analyzable_articles"
        )
    return [dict(row) for row in rows]


async def load_incomplete_articles(database):
    async with database.connect("vector_app") as reader:
        rows = await reader.fetch(
            "SELECT id, source_id, url, observed_article FROM incomplete_articles"
        )
    return [
        {**dict(row), "observed_article": json.loads(row["observed_article"])}
        for row in rows
    ]


async def load_stored_events(database):
    async with database.connect("vector_app") as reader:
        rows = await reader.fetch(
            "SELECT event_type, schema_version, payload FROM outbox_events"
        )
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
