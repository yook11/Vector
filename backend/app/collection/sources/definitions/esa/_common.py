"""ESA Djangoplicity 規格 RSS の取得共通処理。

ESA/Hubble / ESA/Webb は同じ Djangoplicity News module で、RSS は同形式
(RSS 2.0、``<title>`` / ``<link>`` / ``<pubDate>`` / ``<description>``、
``<author>`` / ``<media:*>`` は出ない)。本文は RSS に無く HTML 詳細ページ
側にあるため ``body=None`` で渡す。

写像 (``to_fetched_article``) は純粋 total で degenerate (空 title / 空
link / published 不在) を drop せず素通しし、converter が ``MISSING_TITLE``
/ ``MISSING_URL`` として可視化する (failure-visibility)。500 字 cap は
converter の ``ARTICLE_TITLE_MAX_LENGTH`` 一元、写像は複製しない。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.reader.rss_reader import RssEntry
from app.collection.source_fetch.tools.fetch_tools import FetchTools


def to_fetched_article(entry: RssEntry) -> FetchedArticle:
    """``RssEntry`` → ``FetchedArticle`` の純粋 total 写像。

    Pattern H のため ``body`` は RSS 本文を採らず ``None`` 固定 (HTML 詳細
    ページ側で補完する)。
    """
    return FetchedArticle(
        title=entry.title,
        url=entry.link,
        body=None,
        published_at=entry.published,
    )


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
        yield to_fetched_article(entry)
