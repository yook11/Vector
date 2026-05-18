"""Frontiers Media RSS の取得共通処理。

Frontiers Media は Open Access の学術出版社で、全 journal が同形式の RSS
を提供する (RSS 2.0、``<title>`` / ``<link>`` / ``<pubDate>``、本文は
``<description>`` の abstract 全文。license は全 journal CC BY 4.0)。

``body < 50`` の entry は editorial/correction の空 description とみなし
Entry 化しない (短すぎる本文は補完救済にも流さない)。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator

from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.source_fetch.tools.rss_parser import RssEntry

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化 (body 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """``content_encoded`` と ``summary`` の長い方を本文として採用する。

    Frontiers は ``content`` が空 / 欠落で ``summary`` (description) に
    abstract 全文を載せる。
    """
    content_encoded = entry.content_encoded or ""
    summary = entry.summary or ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


async def frontiers_entries(
    tools: FetchTools,
    *,
    source_name: str,
    endpoint_url: str,
) -> AsyncIterator[FetchedArticle]:
    """Frontiers Media journal RSS の取得共通処理。"""
    entries = await tools.rss.fetch(
        endpoint_url=endpoint_url,
        source_name=source_name,
        parse_mode="bytes",
    )
    for entry in entries:
        title = entry.title[:500]
        if not title:
            continue
        body = _strip_html(_pick_body(entry))
        if len(body) < 50:
            continue  # skip editorial/correction (empty description)
        if entry.published is None:
            continue
        yield FetchedArticle(
            title=title,
            url=entry.link,
            body=body,
            published_at=entry.published,
        )
