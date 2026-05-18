"""Frontiers Media RSS の取得共通処理 (P2-D)。

Frontiers Media は Open Access の学術出版社で、全 journal が同形式の RSS
``https://www.frontiersin.org/journals/{slug}/rss`` を提供する。各 entry の
構造は journal を問わず共通:

- RSS 2.0 (UTF-8)
- ``<item>`` は ``<title>`` (CDATA、論文タイトル) / ``<link>`` (絶対 URL、
  ``/articles/<DOI>`` 形式) / ``<guid>`` (link と同値) / ``<pubDate>``
  (ISO 8601 ``YYYY-MM-DDT00:00:00Z``) / ``<description>`` (abstract 全文、
  1200-1600 chars) / ``<author>`` (corresponding author 1 名) / ``<category>``
  (``Original Research`` / ``Editorial`` 等の **記事種別** で topic ではない)
- ``<content:encoded>`` は出ない (description に abstract 全文)
- ``<media:*>`` は出ない (画像なし)

- **Pattern R** via ``<description>``: abstract 全文 (Pattern R variant、
  eLife と同パターン)
- license: 全 journal CC BY 4.0 (Frontiers open access policy)

P1 まで: 継承基底で subclass が ``NAME`` / ``ENDPOINT_URL`` / ``JOURNAL_NAME``
ClassVar を差替。
P2(B+C): ``FrontiersJournalAdapter`` 汎用 machinery クラス。
P2-D (本実装): Adapter 概念除去。本モジュールは **free function**
``frontiers_entries(tools, *, source_name, endpoint_url)`` として共通処理だけ
を持つ。具体 Source (``Frontiers*Source`` ×4) は ``frontiers/sources.py`` が
宣言し、その ``collect`` が本関数へ委譲する (``_common.py`` に source-specific
な事実を残さない)。journal 識別は ``XxxSource.name`` に一本化。

判定順は旧 ``BaseFrontiersFetcher._convert_entry`` を完全踏襲: title 空 →
body<50 → published None。``body < 50`` drop は editorial/correction の空
description を落とす business critical drop のため共通処理内で実施する
(builder の Incomplete 救済に流さない)。
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
    abstract 全文を載せる。VB / eLife と同形のロジックで吸収する。
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
    """Frontiers Media journal RSS の Pattern R 取得共通処理。"""
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
            continue  # business critical drop (editorial/correction)
        if entry.published is None:
            continue
        yield FetchedArticle(
            title=title,
            url=entry.link,
            body=body,
            published_at=entry.published,
        )
