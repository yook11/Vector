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
- ``<author>``: 単一 (corresponding author 名)、``metadata.author`` に直入れ
- ``<category>``: 記事種別 (Original Research / Review 等) で topic として
  意味を持たない → ``metadata.tags`` に詰めず空 tuple
- license: 全 journal CC BY 4.0 (Frontiers open access policy) を hardcode、
  ``metadata.extras["license"]`` に詰める
- DOI: link から ``10.3389/<prefix>.<year>.<id>`` を正規表現で抽出して
  ``metadata.extras["doi"]`` に詰める (将来昇格候補)
- attribution: news_sources 行の ``attribution_label``
  (``"Frontiers in {Journal} · CC BY 4.0"``)

PROVIDES = ``{"language", "guid", "site_name", "author"}`` 共通。author は
Frontiers の RSS 仕様で必ず提供される (corresponding author hardrequired)。
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
    FetchedArticle,
    FetchedMetadata,
    FetchOutcome,
    ReadyForArticle,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_LANGUAGE = "en"
_LICENSE = "CC BY 4.0"
# DOI URL から DOI 文字列を抽出 (link は
# https://www.frontiersin.org/articles/10.3389/frai.2026.1767330)
_DOI_RE = re.compile(r"10\.3389/[a-z]+\.\d{4}\.\d+")


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


def _extract_guid(entry: dict[str, Any]) -> str | None:
    """``<guid>`` (= link と同値の絶対 URL) を取り出す。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _extract_doi(link_or_guid: str | None) -> str | None:
    """link / guid から ``10.3389/<prefix>.<year>.<id>`` を抽出する。"""
    if not link_or_guid:
        return None
    m = _DOI_RE.search(link_or_guid)
    return m.group(0) if m else None


def _normalize_language(raw: str | None, default: str) -> str:
    """``en`` / ``en-US`` の表記揺れを統一。``raw`` 欠落時は subclass default。"""
    value = (raw or default).replace("_", "-")
    return value[:20]


class BaseFrontiersFetcher:
    """Frontiers Media journal RSS の Pattern R 共通基底。

    subclass は次の 3 つの ClassVar を必須で差し替える:

    - ``NAME``: ``news_sources.name`` 一致
      (``"Frontiers in Artificial Intelligence"`` 等)
    - ``ENDPOINT_URL``: feed URL (``https://www.frontiersin.org/journals/<slug>/rss``)
    - ``JOURNAL_NAME``: human readable journal 名 (``metadata.site_name`` 値)

    PROVIDES = ``{"language", "guid", "site_name", "author"}``。author は
    corresponding author で必ず提供される (RSS 仕様)。

    body / published_at / source_url が品質ゲートを通らない entry は
    ``Failed`` で drop する。Frontiers は editorial/correction 系で
    description が空のことがあるため、``body_too_short`` での drop は正常動作。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]
    JOURNAL_NAME: ClassVar[str]
    LANGUAGE: ClassVar[str] = _DEFAULT_LANGUAGE
    PROVIDES: ClassVar[frozenset[str]] = frozenset(
        {"language", "guid", "site_name", "author"}
    )

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
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

        body = _strip_html(_pick_body(entry))
        if len(body) < 50:
            return Failed(
                reason=FailureReason(
                    code="body_too_short",
                    retryable=False,
                    detail=f"rss_body_len={len(body)}",
                )
            )

        published_at = _parse_published_at(entry)
        if published_at is None:
            return Failed(
                reason=FailureReason(
                    code="published_at_missing",
                    retryable=False,
                    detail="rss_pubdate_missing",
                )
            )

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

        try:
            article = FetchedArticle(
                title=title,
                body=body,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError as e:
            return Failed(
                reason=FailureReason(
                    code="other",
                    retryable=False,
                    detail=f"invariant_violation:{e}",
                )
            )

        author = entry.get("author")
        if isinstance(author, str) and author.strip():
            author = author.strip()[:200]
        else:
            author = None

        guid = _extract_guid(entry)
        extras: dict[str, Any] = {"license": _LICENSE}
        doi = _extract_doi(link) or _extract_doi(guid)
        if doi:
            extras["doi"] = doi

        # Frontiers の <category> は記事種別 (Original Research 等) で topic
        # として意味を持たないため tags には詰めない (空 tuple のまま)
        metadata = FetchedMetadata(
            author=author,
            tags=(),
            image_url=None,
            language=feed_language,
            guid=guid,
            site_name=self.JOURNAL_NAME,
            extras=extras,
        )

        return ReadyForArticle(article=article, metadata=metadata)
