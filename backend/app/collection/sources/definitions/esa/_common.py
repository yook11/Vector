"""ESA Djangoplicity 規格 RSS の取得共通処理。

ESA/Hubble / ESA/Webb は同じ Djangoplicity News module で、RSS は同形式
(RSS 2.0、``<title>`` / ``<link>`` / ``<pubDate>`` / ``<description>``、
``<author>`` / ``<media:*>`` は出ない)。本文は RSS に無く HTML 詳細ページ
側にあるため ``body=None`` で渡す。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools


async def djangoplicity_entries(
    tools: FetchTools,
    *,
    source_name: str,
    endpoint_url: str,
) -> AsyncIterator[FetchedArticle]:
    """ESA Djangoplicity News module RSS の取得共通処理。"""
    entries = await tools.rss.fetch(
        endpoint_url=endpoint_url,
        source_name=source_name,
        parse_mode="bytes",
    )
    for entry in entries:
        title = entry.title[:500]
        if not title:
            continue
        yield FetchedArticle(
            title=title,
            url=entry.link,
            body=None,
            published_at=entry.published,
        )
