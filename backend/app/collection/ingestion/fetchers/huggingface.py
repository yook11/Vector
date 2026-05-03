"""Hugging Face Blog 用 Fetcher — Pattern H、author hardcode、org URL parser。

Phase 3 PR 3-d-2。Hugging Face 公式 blog (`https://huggingface.co/blog/feed.xml`)
は ``<description>`` を提供せず、``<title>`` と ``<link>`` のみで本文は
HTML 詳細ページに完全委譲する Pattern H ソース。

per-source 設計 (実 RSS 観察ベース、SSoT: tier1-fetcher-research.md §PR 3-d-2):

- feed が **RSS 2.0** (UTF-8、HF 自社 CMS)
- ``<item>`` は ``<title>`` / ``<link>`` (絶対 URL) / ``<guid>`` (link と同値) /
  ``<pubDate>`` (RFC 822 GMT)
- ``<description>`` は **空文字** で出力される (HTML 抽出 task で本文取得)
- ``<author>`` / ``<dc:creator>`` / ``<media:*>`` は **未提供**
- ``<category>`` も **未提供** (tags は HTML 抽出時に補完候補)
- 組織発信のため ``metadata.author = "Hugging Face"`` hardcode
- ``<link>`` は次の 2 形式が混在: ``/blog/<slug>`` (HF 公式) / ``/blog/<org>/<slug>``
  (community post)。後者の ``<org>`` を ``metadata.extras = {"hf_org": ...}``
  に詰めて将来の attribution 動的化に備える
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
    FetchedMetadata,
    FetchOutcome,
    PendingHtmlFetch,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_LANGUAGE = "en-US"
_AUTHOR_HARDCODED = "Hugging Face"

# /blog/<org>/<slug> から org を抽出 (slug 部にも `/` が混じる可能性があるため
# 末端を貪欲に捉えず、/blog/ 直後の 1 segment で打ち切る)
_HF_ORG_RE = re.compile(r"^/blog/([^/]+)/[^/]+/?$")
# /blog/<slug> 単独形式 (HF 公式投稿) は org 抽出対象外
_HF_OFFICIAL_RE = re.compile(r"^/blog/[^/]+/?$")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _extract_guid(entry: dict[str, Any]) -> str | None:
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _extract_hf_org(link: str) -> str | None:
    """``<link>`` から HF community org を抽出する純関数。

    HF 公式投稿 (``/blog/<slug>``) の場合は None を返す。Community 投稿
    (``/blog/<org>/<slug>``) の場合は ``<org>`` を返す。

    URL parse の手抜きを避けるため正規表現で path を絞る (``//evil/blog/x/y``
    のような細工をはじき、host が huggingface.co 以外でも path 部のみ判定)。
    """
    if not link:
        return None
    # `https://huggingface.co/blog/foo` の path 部分のみを切り出す
    try:
        from urllib.parse import urlparse

        path = urlparse(link).path
    except (ValueError, TypeError):
        return None
    if _HF_OFFICIAL_RE.match(path):
        return None
    m = _HF_ORG_RE.match(path)
    if m:
        return m.group(1)[:100]  # 防御的 cap
    return None


def _normalize_language(raw: str | None) -> str:
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class HuggingFaceBlogFetcher:
    """Hugging Face Blog 用 Pattern H Fetcher。

    PROVIDES に列挙したフィールドは feed-level / RSS 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``<language>en-US</language>``
    - ``guid``: ``<guid>`` (link と同値、安定 ID)
    - ``site_name``: hardcode "Hugging Face"
    - ``author``: hardcode "Hugging Face" (組織発信)
    """

    NAME: ClassVar[str] = "Hugging Face"
    ENDPOINT_URL: ClassVar[str] = "https://huggingface.co/blog/feed.xml"
    PROVIDES: ClassVar[frozenset[str]] = frozenset(
        {"language", "guid", "site_name", "author"}
    )

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "huggingface_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        feed_language = _normalize_language(feed.feed.get("language"))

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

        org = _extract_hf_org(link)
        extras: dict[str, Any] | None = {"hf_org": org} if org else None

        metadata = FetchedMetadata(
            author=_AUTHOR_HARDCODED,
            tags=(),
            image_url=None,
            language=feed_language,
            guid=_extract_guid(entry),
            site_name=self.NAME,
            extras=extras,
        )

        return PendingHtmlFetch(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
            metadata=metadata,
        )
