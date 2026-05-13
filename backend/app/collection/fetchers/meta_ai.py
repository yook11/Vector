"""Meta AI 用 Fetcher (Phase 3 PR 3-d-3) — about.fb.com から AI 関連のみ抽出。

per-source 設計:

- ENDPOINT: ``https://about.fb.com/news/feed/`` (Meta Newsroom)。``ai.meta.com``
  は専用 RSS / sitemap 一切提供なしのため代替経路として採用。
- RSS 2.0 + dc/content/media WordPress 標準。``<content:encoded>`` に full
  body (~3-4K chars) → Pattern R。
- **AI tag フィルタ必須**: Newsroom は WhatsApp / Threads / Sustainability 等
  全社カテゴリが流入する (実測 10 件中 6 件のみ AI tagged)。``<category>``
  集合に ``"AI"`` を含む entry のみ採用、それ以外は
  ``SourceFetchFailed(detail="not_ai_tagged")`` で構造的に drop する
  (business critical)。
- ``<dc:creator>``: 大半が "Facebook" 固定 → ``metadata.author``
- ``<media:content>`` 多重 → 先頭画像を ``image_url``
- attribution_label: ``"Meta Newsroom"``
- PROVIDES = {language, guid, site_name}
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

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
_DEFAULT_LANGUAGE = "en-US"

# AI 関連と判定する category 集合 (大文字小文字区別)。Meta Newsroom の
# `<category>` は "AI" / "Technology and Innovation" 等が混在するため、
# 厳密に "AI" tag を含むものだけを採用する (off-topic 取り込み防止)。
_AI_TAGS: Final[frozenset[str]] = frozenset({"AI"})


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: dict[str, Any]) -> str:
    """``<content:encoded>`` と ``<description>`` の長い方を本文として採用。"""
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
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _extract_tags(entry: dict[str, Any]) -> tuple[str, ...]:
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return ()
    return tuple(
        t["term"]
        for t in tags
        if isinstance(t, dict) and isinstance(t.get("term"), str) and t["term"]
    )


def _is_ai_tagged(tags: tuple[str, ...]) -> bool:
    """``tags`` に AI 判定 tag が含まれているか。"""
    return bool(_AI_TAGS.intersection(tags))


def _extract_image_url(entry: dict[str, Any]) -> SafeUrl | None:
    """``<media:content>`` 多重から先頭画像を取り出す (probabilistic)。"""
    media = entry.get("media_content")
    if not isinstance(media, list) or not media:
        return None
    first = media[0]
    if not isinstance(first, dict):
        return None
    url = first.get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        return SafeUrl(url)
    except ValueError:
        return None


def _extract_guid(entry: dict[str, Any]) -> str | None:
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class MetaAIFetcher:
    """about.fb.com Newsroom から AI tagged entry のみを抽出する Pattern R Fetcher。

    AI フィルタ業務ロジックがクリティカル: Newsroom は全社混在で約 60% が
    非 AI 記事。spec の AI tag フィルタを構造的に絞り込み、off-topic 取り込み
    (=ニュース文脈ノイズ) を抑止する。
    """

    NAME: ClassVar[str] = "Meta AI"
    ENDPOINT_URL: ClassVar[str] = "https://about.fb.com/news/feed/"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "meta_ai_feed_parse_error",
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
        # AI tag フィルタを最初に適用 (他フィールドの parse コストを節約)
        tags = _extract_tags(entry)
        if not _is_ai_tagged(tags):
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="other",
                    retryable=False,
                    detail="not_ai_tagged",
                )
            )

        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="title_missing",
                    retryable=False,
                    detail="rss_title_missing",
                )
            )
        title = title[:500]

        body = _strip_html(_pick_body(entry))
        if len(body) < 50:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="body_too_short",
                    retryable=False,
                    detail=f"rss_body_len={len(body)}",
                )
            )

        published_at = _parse_published_at(entry)
        if published_at is None:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="published_at_missing",
                    retryable=False,
                    detail="rss_pubdate_missing",
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

        author = entry.get("author")
        if isinstance(author, str) and author:
            author = author[:200]
        else:
            author = None

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
        }
        if author:
            metadata["author"] = author
        if tags:
            metadata["tags"] = list(tags)
        if image_url := _extract_image_url(entry):
            metadata["image_url"] = str(image_url)
        if guid := _extract_guid(entry):
            metadata["guid"] = guid

        return FetchedEntry(item=ready, metadata=metadata)
