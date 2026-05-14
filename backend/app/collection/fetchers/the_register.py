"""The Register 用 Fetcher — Pattern R+H (Pattern H 設計で実装、Atom feed)。

collection-acquisition-redesign Phase 1d。The Register の Atom フィードは
``<summary>`` に短いリード文しか出さず、本文は HTML を別途取得して
trafilatura で抽出する必要がある (`spec collection-source-rss-research.md`
の Pattern R+H 分類)。

per-source 設計 (実 Atom 観察ベース):

- feed 形式は **Atom (RFC4287)**、``xml:lang="en"``
- ``<link rel="alternate" href>`` は **redirector URL**
  (``https://go.theregister.com/feed/<host>/<path>``)、
  ``_normalize_register_link`` で実 URL に展開してから ``SafeUrl`` 構築する
- language は feed-level ``xml:lang="en"`` (NOT en-US)
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

from app.collection.article.domain.value_objects import PublishedAt
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

_REDIRECTOR_PREFIX = "https://go.theregister.com/feed/"


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (title 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Atom の ``<published>`` ISO 8601 (例: ``2026-05-01T21:39:10.00Z``) は
    feedparser が標準解釈する。Pattern H 固有: 本値が None でも drop しない
    (HTML 補完を待つ)。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _normalize_register_link(raw: str) -> str:
    """``go.theregister.com/feed/<host>/<path>`` → ``https://<host>/<path>`` に直す。

    The Register の Atom フィードは ``<link href>`` がリダイレクタ経由
    (``https://go.theregister.com/feed/www.theregister.com/2026/...``) のため、
    プレフィックスを切り捨てて実 URL を再構築する (memory
    `project_the_register_fetcher_decision.md` の split case C、実観察で 100%
    一貫を確認済)。
    """
    if raw.startswith(_REDIRECTOR_PREFIX):
        return "https://" + raw[len(_REDIRECTOR_PREFIX) :]
    return raw


class TheRegisterFetcher:
    """The Register 用 Pattern H Fetcher (Pattern R+H = HTML 必須、Atom feed)。"""

    NAME: ClassVar[str] = "The Register"
    ENDPOINT_URL: ClassVar[str] = "https://www.theregister.com/headlines.atom"

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "the_register_feed_parse_error",
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

    async def _fetch_feed(self) -> str:
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
            return response.text

    def _convert_entry(
        self,
        entry: dict[str, Any],
        source_id: int,
    ) -> IncompleteArticle | None:
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return None
        title = title[:500]

        raw_link = entry.get("link", "") or ""
        if not raw_link:
            return None
        normalized_link = _normalize_register_link(raw_link)
        try:
            source_url = SafeUrl(normalized_link)
        except ValueError:
            return None

        published_at_hint = _parse_published_at(entry)

        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
        )
