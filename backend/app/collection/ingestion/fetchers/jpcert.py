"""JPCERT/CC 用 Fetcher — Pattern H (RSS で URL 列挙、本文は HTML 必須)。

collection-acquisition-redesign Phase 1c-E (Pattern H 最終)。JPCERT/CC は
政府系 CSIRT の RSS で、他 Pattern H ソースと比べて以下の固有点を持つ:

per-source 設計 (実 RSS 観察ベース):

- feed が **RDF (RSS 1.0)** ルート (``<rdf:RDF>``)。feedparser は
  ``feed.version="rss10"`` として標準解釈する
- ``<item rdf:about="URL">`` の URL を feedparser が ``entry.id`` にマップ
  するため、guid 抽出は標準 ``_extract_guid`` で OK
- ``<title>`` は **多行 + インデント空白** が含まれるため ``_strip_html``
  で whitespace 正規化する (HTML タグ自体は通常含まれないが defensive)
- "注意喚起:" / "[公開]" 等の接頭辞は **コンテンツ本体** であり ITmedia
  AI+ ``[ITmedia ...]`` のような navigational noise ではないため strip
  しない (情報の重要度を保持)
- ``<dc:date>`` が ISO 8601 ("2026-04-27T10:47+09:00") のため feedparser が
  ``published_parsed`` を確実に populate する → strptime fallback 不要
- per-entry の ``<dc:creator>`` / ``<author>`` / ``<category>`` /
  ``<media:*>`` は **すべて未提供** のため metadata は ``None`` / ``()``
  直書き (channel-level の ``<dc:creator>`` = webmaster@... は entry author
  ではないため使わない)
- ``<description>`` / ``<content:encoded>`` は item により完全空のことが
  多いため body は読まない (Pattern H、Stage 2 = HTML 抽出の責務)

旧 ``fetchers/rss/jpcert.py`` (BaseRssFetcher 継承の薄スタブ) は本 PR で
削除し、新 Protocol に置き換える。これにより Pattern H は 8/8 完全移行。
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
_DEFAULT_LANGUAGE = "ja"


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 多行 / インデント空白の正規化 (title 用)。

    JPCERT の ``<title>`` は通常 HTML タグを含まないが、改行 + 全角/半角
    インデント空白で多行化されているため ``\\s+`` 1 個化が必要。本関数を
    defensive に通すことで title が常に 1 行 plain text に揃う。
    """
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    JPCERT/CC は ``<dc:date>`` ISO 8601 を提供するため feedparser 標準経路で
    解釈可能 (FB のような strptime fallback は不要)。Pattern H 固有: 本値が
    None でも Failed 降格はしない (HTML 抽出が ``published_at`` を出して
    くれれば try_advance_from で merge 後に最終確定)。
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
    """``<item rdf:about="URL">`` を feedparser がマップした ``entry.id`` を返す。

    JPCERT の RDF item は ``rdf:about`` を必須属性として持ち、feedparser は
    これを ``entry.id`` (互換のため ``entry.guid`` でも参照可) に格納する。
    結果として guid は item の URL と同値となるが、一意識別子として有効。
    """
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    """``ja`` / ``ja-JP`` 等の表記揺れを統一。JPCERT は xml:lang ``"ja"`` 想定。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class JPCERTFetcher:
    """JPCERT/CC 用 Pattern H Fetcher。

    PROVIDES に列挙したフィールドは feed-level / RDF 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``xml:lang="ja"``
    - ``guid``: ``<item rdf:about="URL">`` (RDF 必須属性)
    - ``site_name``: hardcode "JPCERT/CC"

    ``author`` / ``tags`` / ``image_url`` は per-entry で未提供のため metadata
    に詰めるが PROVIDES には含めない。
    """

    NAME: ClassVar[str] = "JPCERT/CC"
    ENDPOINT_URL: ClassVar[str] = "https://www.jpcert.or.jp/rss/jpcert.rdf"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "jpcert_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        feed_language = _normalize_language(feed.feed.get("language"))

        for entry in feed.entries:
            yield self._convert_entry(entry, source_id, feed_language)

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
        feed_language: str,
    ) -> FetchOutcome:
        """1 entry を ``FetchOutcome`` に変換する純関数。

        Pattern H 固有の品質ゲート:

        - ``title`` 空 → ``Failed(title_missing)``
        - ``link`` 不正 → ``Failed(extraction_empty)``
        - ``published_at`` 欠落 → **Failed しない** (HTML 補完を待つ)
        - ``body`` は本実装では検査しない (Stage 2 の責務)
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

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
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
