"""ESA Djangoplicity 規格 RSS Fetcher の共通基底 (Phase 3 PR 3-b)。

Djangoplicity の News module は ESA/Hubble / ESA/Webb / ESO / ALMA で広く
使われる科学広報 CMS。RSS 出力は構造的に同型:

- RSS 2.0 (UTF-8、CET/CEST timezone の pubDate)
- ``<item>`` は ``<title>`` (CDATA, "Photo Release:" / "Science Release:" 等
  の prefix を含む) / ``<link>`` (絶対 URL) / ``<guid>`` (link と同値) /
  ``<pubDate>`` (RFC 822 +0100/+0200) / ``<description>`` (HTML の lead
  paragraph、~500-900 chars)
- ``<author>`` / ``<dc:creator>`` / ``<media:*>`` は **未提供**
- 本文は HTML 詳細ページに委譲 (Pattern H)

per-source 設計:

- author は機関発信のため hardcode (subclass の ``AUTHOR`` で指定)
- site_name は subclass の ``SITE_NAME`` で指定 ("ESA/Hubble" / "ESA/Webb")
- language は feed-level ``<language>en</language>`` を使うが、欠落保険
  として ``LANGUAGE`` ClassVar (default ``"en"``) を持つ
- image は RSS では取れず HTML 抽出に委譲 (PROVIDES から除外)

PROVIDES = ``{"language", "guid", "site_name", "author"}`` 共通。
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

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FailureReason,
    FetchedEntry,
    FetchOutcome,
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


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化 (title 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Djangoplicity は CET/CEST (+0100/+0200) で出力するため、feedparser の
    UTC 正規化結果を ``PublishedAt`` に詰めれば自動的に時差が吸収される。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _extract_guid(entry: dict[str, Any]) -> str | None:
    """``<guid>`` (= link と同値の絶対 URL) を取り出す。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None, default: str) -> str:
    """``en`` / ``en-US`` の表記揺れを統一。``raw`` 欠落時は subclass default。"""
    value = (raw or default).replace("_", "-")
    return value[:20]


class BaseDjangoplicityFetcher:
    """ESA Djangoplicity News module の Pattern H 共通基底。

    subclass は次の 3 つの ClassVar を必須で差し替える:

    - ``NAME``: ``news_sources.name`` 一致 ("ESA/Hubble" / "ESA/Webb")
    - ``ENDPOINT_URL``: feed URL ("https://esahubble.org/news/feed/" 等)
    - ``SITE_NAME``: ``metadata.site_name`` 値 (NAME と同値で OK)
    - ``AUTHOR``: ``metadata.author`` hardcode (機関発信、PROVIDES に含む)

    ``LANGUAGE`` は default ``"en"`` のままで OK (feed-level ``<language>``
    が確実に ``en`` を返す観察ベース)。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]
    SITE_NAME: ClassVar[str]
    AUTHOR: ClassVar[str]
    LANGUAGE: ClassVar[str] = "en"
    PROVIDES: ClassVar[frozenset[str]] = frozenset(
        {"language", "guid", "site_name", "author"}
    )

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "djangoplicity_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        feed_language = _normalize_language(
            feed.feed.get("language"), default=self.LANGUAGE
        )

        for entry in feed.entries:
            yield self._convert_entry(entry, source_id, feed_language)

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
        feed_language: str,
    ) -> FetchOutcome:
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return Failed(
                reason=FailureReason(
                    code="title_missing",
                    retryable=False,
                    detail="rss_title_missing",
                )
            )
        title = title[:500]

        link = entry.get("link", "") or ""
        try:
            source_url = SafeUrl(link)
        except ValueError:
            return Failed(
                reason=FailureReason(
                    code="extraction_empty",
                    retryable=False,
                    detail=f"invalid_link:{link[:100]}",
                )
            )

        published_at_hint = _parse_published_at(entry)

        metadata: dict[str, Any] = {
            "author": self.AUTHOR,
            "language": feed_language,
            "site_name": self.SITE_NAME,
        }
        if guid := _extract_guid(entry):
            metadata["guid"] = guid

        return FetchedEntry(
            item=IncompleteArticle(
                title=title,
                source_id=source_id,
                source_url=source_url,
                published_at_hint=published_at_hint,
            ),
            metadata=metadata,
        )
