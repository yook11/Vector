"""Frontiers Media RSS Fetcher の共通基底 (Phase 3 PR 3-c-3)。

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

import asyncio
import html
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import feedparser
import httpx
import structlog

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_LANGUAGE = "en"


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: dict[str, Any]) -> str:
    """``<content:encoded>`` と ``<description>`` の長い方を本文として採用する。

    Frontiers は ``content`` が空 / 欠落で ``summary`` (description) に
    abstract 全文を載せる。VB / eLife と同形のロジックで吸収する。
    """
    content_encoded = ""
    contents = entry.get("content")
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict):
            value = first.get("value")
            if isinstance(value, str):
                content_encoded = value
    summary = entry.get("summary") or ""
    if not isinstance(summary, str):
        summary = ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。"""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


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

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "frontiers_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        for entry in feed.entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

    async def _fetch_feed(self) -> bytes:
        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(self.ENDPOINT_URL)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {self.NAME}") from e
                raise TemporaryFetchError(f"HTTP {status}: {self.NAME}") from e
            except httpx.RequestError as e:
                raise TemporaryFetchError(f"request error: {self.NAME}: {e}") from e
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e
            return response.content

    def _convert_entry(
        self,
        entry: dict[str, Any],
        source_id: int,
    ) -> ReadyForArticle | None:
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return None
        title = title[:500]

        body = _strip_html(_pick_body(entry))
        if len(body) < 50:
            return None

        published_at = _parse_published_at(entry)
        if published_at is None:
            return None

        link = entry.get("link", "") or ""
        try:
            source_url = SafeUrl(link)
        except ValueError:
            return None

        try:
            return ReadyForArticle(
                title=title,
                body=body,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            return None
