"""MIC (総務省) 用 Fetcher — Pattern H、RDF (RSS 1.0)、Shift_JIS。

Phase 3 PR 3-h-1。総務省の新着情報 RDF feed (`/news.rdf`) を MEXT 同型の
Pattern H で扱う。Tier 1 ソースで唯一の **Shift_JIS** エンコード。

per-source 設計 (実 RSS 観察ベース、SSoT: tier1-fetcher-research.md §PR 3-h-1):

- feed が **RDF (RSS 1.0)**、宣言が ``<?xml encoding="Shift_JIS"?>``
- ``feedparser.parse(response.content)`` (bytes) 必須。``response.text`` は
  httpx の charset 推定 (UTF-8 fallback) で文字化けが起きる。bytes 経由なら
  feedparser が XML 宣言から正しく Shift_JIS を sniff する
- ``<item rdf:about="URL">`` の URL を feedparser が ``entry.id`` にマップ
- ``<description>`` が ``<title>`` と同一 (本文ゼロ) のことが多い。Pattern H
  なので summary は metadata に格納しない設計に揃え、本文は HTML 抽出に委譲
- ``<dc:date>`` ISO 8601 → feedparser 標準経路で ``published_parsed`` を populate
- per-entry の author / tags / image_url は **未提供** → None / () 直書き
- robots.txt: ``ia_archiver`` のみ Disallow、Vector は対象外、defensive で
  60s 以上の crawl interval を確保
- License: 政府標準利用規約 + ODC-By v1.0 互換、attribution は news_sources
  行の ``attribution_label`` カラムに格納
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
_DEFAULT_LANGUAGE = "ja"


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化 (title 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Pattern H 固有: 本値が None でも Failed 降格はしない (HTML 抽出時に
    補完される)。
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
    """``<item rdf:about="URL">`` を feedparser がマップした ``entry.id`` を返す。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    """``ja`` / ``ja-JP`` の表記揺れを統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class MICFetcher:
    """MIC (総務省) 用 Pattern H Fetcher。Shift_JIS RDF 対応。

    PROVIDES に列挙したフィールドは feed-level / RDF 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``xml:lang="ja"``
    - ``guid``: ``<item rdf:about="URL">`` (RDF 必須属性)
    - ``site_name``: hardcode "MIC"
    """

    NAME: ClassVar[str] = "MIC"
    ENDPOINT_URL: ClassVar[str] = "https://www.soumu.go.jp/news.rdf"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "mic_feed_parse_error",
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
        """feed を取得して **bytes** を返す。Shift_JIS 対応のため必須。

        ``response.text`` は httpx が ``Content-Type: charset=`` から推定 (なければ
        UTF-8 fallback) するため、Shift_JIS 宣言が無視され文字化けする。bytes
        経由なら feedparser が XML 宣言の encoding を sniff する。
        """
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
        """1 entry を ``FetchOutcome`` に変換する純関数。

        Pattern H 固有の品質ゲート:

        - ``title`` 空 → ``Failed(title_missing)``
        - ``link`` 不正 → ``Failed(extraction_empty)``
        - ``published_at`` 欠落 → **Failed しない** (HTML 補完を待つ)
        - ``body`` は本実装では検査しない (Stage 2 の責務)
        - ``description == title`` でも本実装では summary を metadata に
          載せない (Pattern H 統一設計)。重複情報を Tier 2 に持つ意味なし
        """
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

        metadata = FetchedMetadata(
            author=None,
            tags=(),
            image_url=None,
            language=feed_language,
            guid=_extract_guid(entry),
            site_name=self.NAME,
        )

        return PendingHtmlFetch(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
            metadata=metadata,
        )
