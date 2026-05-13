"""eLife 用 Fetcher (Phase 3 PR 3-c-2) — Pattern R via ``<description>``。

per-source 設計:

- RSS 2.0 + ``<webfeeds>`` / ``<dc>`` 拡張、``<content:encoded>`` は出ない
  (本文相当は ``<description>`` 1500 字程度)。``<description>`` を本文として
  採用する Pattern R の variant。
- 多重 ``<author>`` (``"<email> (<name>)"`` format) → ``metadata.authors``
  に氏名のみ抽出して tuple 化、重複除去。``metadata.author`` には先頭
  著者の氏名を載せる (caller の単純表示用)。
- ``<webfeeds:featuredImage>`` → ``metadata.image_url`` 候補
  (eLife は CDN ロゴを返すが構造的に拾う)。
- license は CC BY 4.0 で全件統一 (eLife open access policy) → ``extras``
  に hardcode。``<guid>`` の DOI も ``extras`` に詰めて将来昇格候補化。
- PROVIDES = {language, guid, site_name} (VB と同じ最小集合、author 系は
  probabilistic 扱い)
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
# DOI URL から DOI 文字列を抽出 (例: https://dx.doi.org/10.7554/eLife.108439)
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s]+$")
# author = "email@x.com (Name)" / "Name" / "" → 氏名のみ抽出
_AUTHOR_PARENS_RE = re.compile(r"^[^@\s]+@\S+\s*\((.+)\)\s*$")


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: dict[str, Any]) -> str:
    """``<content:encoded>`` と ``<description>`` の長い方を本文として採用する。

    eLife は実 feed では ``content`` が空 / 欠落で ``summary`` (description) に
    abstract 全文を載せる。VB と同形のロジックで吸収する。
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


def _extract_image_url(entry: dict[str, Any]) -> SafeUrl | None:
    """``<webfeeds:featuredImage>`` から画像 URL を取り出す (probabilistic)。"""
    image = entry.get("webfeeds_featuredimage")
    if not isinstance(image, dict):
        return None
    url = image.get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        return SafeUrl(url)
    except ValueError:
        return None


def _extract_tags(entry: dict[str, Any]) -> tuple[str, ...]:
    """feedparser の ``tags`` (= ``<category>``) を tuple 化する。"""
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return ()
    return tuple(
        t["term"]
        for t in tags
        if isinstance(t, dict) and isinstance(t.get("term"), str) and t["term"]
    )


def _extract_guid(entry: dict[str, Any]) -> str | None:
    """``<guid>`` (eLife は DOI URL) を取り出す。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _extract_doi(guid: str | None) -> str | None:
    """``<guid>`` (DOI URL) から DOI 文字列 (``10.7554/eLife.<id>``) を抽出。"""
    if not guid:
        return None
    m = _DOI_RE.search(guid)
    return m.group(0) if m else None


def _parse_author_name(raw: str) -> str | None:
    """``"email@x.com (Name)"`` 形式から氏名を抽出。失敗時は raw を返す。"""
    if not raw:
        return None
    m = _AUTHOR_PARENS_RE.match(raw.strip())
    if m:
        return m.group(1).strip()
    return raw.strip()


def _extract_authors(entry: dict[str, Any]) -> tuple[str, ...]:
    """``<author>`` 多重 (feedparser ``authors`` list) を氏名 tuple に正規化。

    feedparser の ``authors`` は ``[{"name": "..."}, ...]`` 形式 (eLife では
    多くが name のみ提供、email は corresponding author 共有で重複)。重複氏名は
    順序保持しつつ除去する。
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


def _normalize_language(raw: str | None) -> str:
    """``en_US`` / ``en-us`` / ``en-US`` の表記揺れを ``en-US`` 系に統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class ELifeFetcher:
    """eLife (latest articles RSS) 用 Pattern R Fetcher。

    PROVIDES は VB と同じ最小集合 (language / guid / site_name)。authors /
    image_url は probabilistic 扱いで metadata に詰めるが PROVIDES には含めない。
    """

    NAME: ClassVar[str] = "eLife"
    ENDPOINT_URL: ClassVar[str] = "https://elifesciences.org/rss/recent.xml"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "elife_feed_parse_error",
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
            ready = ReadyForArticle(
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

        authors = _extract_authors(entry)
        # 単独 author は authors の先頭 (feedparser の `author` は email 形式の
        # ことがあるため `authors` 由来の整形済みの方が表示に向く)
        primary_author: str | None = authors[0] if authors else None
        if primary_author is None:
            raw_author = entry.get("author")
            if isinstance(raw_author, str):
                primary_author = _parse_author_name(raw_author)
                if primary_author is not None:
                    primary_author = primary_author[:200]

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
        if tags := _extract_tags(entry):
            metadata["tags"] = list(tags)
        if image_url := _extract_image_url(entry):
            metadata["image_url"] = str(image_url)
        if guid:
            metadata["guid"] = guid
        if doi := _extract_doi(guid):
            metadata["doi"] = doi

        return FetchedEntry(item=ready, metadata=metadata)
