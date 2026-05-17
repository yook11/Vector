"""Frontiers Media RSS の取得 machinery (P2)。

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

per-source 設計:

- **Pattern R** via ``<description>``: abstract 全文 (Pattern R variant、
  eLife と同パターン)
- license: 全 journal CC BY 4.0 (Frontiers open access policy)
- attribution: news_sources 行の ``attribution_label``
  (``"Frontiers in {Journal} · CC BY 4.0"``)

P1 までは継承基底で subclass が ``NAME`` / ``ENDPOINT_URL`` /
``JOURNAL_NAME`` ClassVar を差し替える形だった。P2 で per-source 知識は
``ArticleSource`` 集約へ移し、本クラスは Source 定義
(``source_name`` / ``endpoint_url``) を ``__init__`` で受け取る汎用 machinery
になった。``JOURNAL_NAME`` は取得 logic に一切寄与しない attribution メタ
だったため、journal 識別は ``ArticleSource.name`` に一本化した。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser

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


class FrontiersJournalAdapter:
    """Frontiers Media journal RSS の Pattern R 取得 machinery (P2)。

    判定順は旧 ``BaseFrontiersFetcher._convert_entry`` を完全踏襲: title 空 →
    body<50 → published None。``body < 50`` drop は editorial/correction の空
    description を落とす business critical drop のため machinery 内で実施する
    (builder の Incomplete 救済に流さない)。``source_name`` / ``endpoint_url``
    は ``ArticleSource.adapter_factory`` から受け取る。
    """

    def __init__(
        self,
        *,
        source_name: str,
        endpoint_url: str,
        parser: RssParser | None = None,
    ) -> None:
        self._source_name = source_name
        self._endpoint_url = endpoint_url
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self._endpoint_url,
            source_name=self._source_name,
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
