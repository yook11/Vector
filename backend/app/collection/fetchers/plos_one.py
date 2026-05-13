"""PLOS ONE 用 Fetcher (Phase 3 PR 3-c-1) — Atom 1.0 Pattern R。

per-source 設計:

- Atom 1.0 (Tier 1 ソース中で唯一の Atom feed)。``<content type="html">``
  に abstract 本文 (1.4K-3K chars 平均)、``<id>`` は DOI 文字列のみ
  (eLife の URL 形式と異なる)。
- 多重 ``<author><name>`` → ``metadata.authors`` tuple 化 + 重複除去。
  ``metadata.author`` は先頭著者。
- ``<rights>`` は feed-level の概要文 (entry-level rights は提供されない)。
  PLOS One は CC BY 4.0 で全件統一 (open access policy) → ``extras`` に
  ``"license"`` を hardcode。``<id>`` の DOI も ``extras["doi"]`` に詰める。
- 言語は feed が宣言しない → ``"en"`` を hardcode。
- editorial note 等の短い entry (~30 chars) は body_too_short で構造的に drop
  される (Pattern R の品質ゲート活用)。
- PROVIDES = {language, guid, site_name} (eLife / VB と同じ最小集合)
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
from app.collection.fetchers.outcome import (
    FetchedEntry,
    FetchOutcome,
    SourceFetchFailed,
    SourceFetchFailureReason,
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
# PLOS DOI format: 10.1371/journal.pone.<id>
_DOI_RE = re.compile(r"^10\.1371/journal\.[a-z]+\.\d+$")


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: dict[str, Any]) -> str:
    """Atom ``<content>`` (= feedparser の ``content[0].value``) を本文として採用。

    Atom 仕様上 ``<content>`` が一級要素で、``<summary>`` は短縮形。Pattern R
    としては content を優先採用すれば十分。
    """
    contents = entry.get("content")
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict):
            value = first.get("value")
            if isinstance(value, str):
                return value
    summary = entry.get("summary")
    return summary if isinstance(summary, str) else ""


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """Atom ``published``/``updated`` を UTC ``PublishedAt`` に変換。

    feedparser は Atom の ISO 8601 (Z 終端) を struct_time に正規化済み。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _extract_authors(entry: dict[str, Any]) -> tuple[str, ...]:
    """Atom ``<author><name>`` 多重 (feedparser ``authors``) を氏名 tuple に。

    順序保持 dedup。
    """
    authors = entry.get("authors")
    if not isinstance(authors, list):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for a in authors:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if not isinstance(name, str):
            continue
        clean = name.strip()[:200]
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return tuple(result)


def _extract_guid(entry: dict[str, Any]) -> str | None:
    """``<id>`` (PLOS では DOI 文字列) を取り出す。"""
    raw = entry.get("id")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _extract_doi(guid: str | None) -> str | None:
    """``<id>`` (PLOS DOI 文字列) が ``10.1371/journal.<j>.<id>`` 形式か検証。"""
    if not guid:
        return None
    return guid if _DOI_RE.match(guid) else None


class PLOSOneFetcher:
    """PLOS ONE 用 Atom Fetcher (Pattern R)。

    PROVIDES は eLife / VB と同じ最小集合。authors / image_url は probabilistic。
    """

    NAME: ClassVar[str] = "PLOS ONE"
    ENDPOINT_URL: ClassVar[str] = "https://journals.plos.org/plosone/feed/atom"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "plos_one_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        for entry in feed.entries:
            yield self._convert_entry(entry, source_id, _DEFAULT_LANGUAGE)

    async def _fetch_feed(self) -> bytes:
        """feed を取得して bytes を返す (feedparser に encoding sniff を委譲)。"""
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
        """1 entry を ``FetchOutcome`` に変換する純関数 (テスト容易性のため切出)。"""
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="title_missing",
                    retryable=False,
                    detail="atom_title_missing",
                )
            )
        title = title[:500]

        body = _strip_html(_pick_body(entry))
        if len(body) < 50:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="body_too_short",
                    retryable=False,
                    detail=f"atom_body_len={len(body)}",
                )
            )

        published_at = _parse_published_at(entry)
        if published_at is None:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="published_at_missing",
                    retryable=False,
                    detail="atom_pubdate_missing",
                )
            )

        link = entry.get("link", "") or ""
        try:
            source_url = SafeUrl(link)
        except ValueError:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="extraction_empty",
                    retryable=False,
                    detail=f"invalid_link:{link[:100]}",
                )
            )

        try:
            ready = ReadyForArticle(
                title=title,
                body=body,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError as e:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="other",
                    retryable=False,
                    detail=f"invariant_violation:{e}",
                )
            )

        authors = _extract_authors(entry)
        primary_author = authors[0] if authors else None
        guid = _extract_guid(entry)

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
            "license": _LICENSE,
        }
        if primary_author:
            metadata["author"] = primary_author
        if authors:
            metadata["authors"] = list(authors)
        if guid:
            metadata["guid"] = guid
        if doi := _extract_doi(guid):
            metadata["doi"] = doi

        return FetchedEntry(item=ready, metadata=metadata)
