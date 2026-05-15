"""Frontiers Media RSS Fetcher の共通基底。

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
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_LANGUAGE = "en"


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


class BaseFrontiersFetcher:
    """Frontiers Media journal RSS の Pattern R 共通基底。

    subclass は次の 3 つの ClassVar を必須で差し替える:

    - ``NAME``: ``news_sources.name`` 一致
      (``"Frontiers in Artificial Intelligence"`` 等)
    - ``ENDPOINT_URL``: feed URL (``https://www.frontiersin.org/journals/<slug>/rss``)
    - ``JOURNAL_NAME``: human readable journal 名 (内部の構造化ログに利用)

    body / published_at / source_url が品質ゲートを通らない entry は yield
    しない (Outcome 純化原則)。Frontiers は editorial/correction 系で description
    が空のことがあるため、品質ゲート未達での drop は正常動作。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]
    JOURNAL_NAME: ClassVar[str]
    LANGUAGE: ClassVar[str] = _DEFAULT_LANGUAGE

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="bytes",
        )
        for entry in entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

    def _convert_entry(
        self,
        entry: RssEntry,
        source_id: int,
    ) -> ReadyForArticle | None:
        title = entry.title[:500]
        if not title:
            return None

        body = _strip_html(_pick_body(entry))
        if len(body) < 50:
            return None

        if entry.published is None:
            return None

        try:
            source_url = CanonicalArticleUrl(entry.link)
        except ValueError:
            return None

        try:
            return ReadyForArticle(
                title=title,
                body=body,
                published_at=PublishedAt(value=entry.published),
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            return None


class BaseFrontiersJournalAdapter:
    """Frontiers Media journal RSS の Pattern R SourceAdapter 共通基底。

    subclass は ``NAME`` / ``ENDPOINT_URL`` / ``JOURNAL_NAME`` の 3 ClassVar を
    必須で差し替える (MDPI base+subclass と同形)。判定順は旧
    ``BaseFrontiersFetcher._convert_entry`` を完全踏襲: title 空 → body<50 →
    published None。``body < 50`` drop は editorial/correction の空 description
    を落とす business critical drop のため Adapter 内で実施する (builder の
    Incomplete 救済に流さない)。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]
    JOURNAL_NAME: ClassVar[str]
    LANGUAGE: ClassVar[str] = _DEFAULT_LANGUAGE

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
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
