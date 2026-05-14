"""VentureBeat 用 Fetcher — Pattern R (RSS-only) の参照実装。

collection-acquisition-redesign Phase 1a'。新 ``Fetcher`` Protocol を満たし、
``ReadyForArticle`` を直接 yield する (品質ゲート未達 entry は yield しない:
Outcome 純化原則)。

VB の RSS feed は ``<description>`` / ``<content:encoded>`` に full body
(~12000 chars) を含み、HTML 取得を経由せずに本文を構築できる
(`spec collection-source-rss-research.md`)。これにより VB が時折 Vercel
Challenge で 5xx を返す問題を構造的に回避する。
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


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する。

    VB の `<description>` / `<content:encoded>` は WordPress 出力で通常
    ``<p>`` / ``<a>`` 程度しか含まないため、tag を空白に置換 → HTML entity を
    decode → 連続空白を 1 つに圧縮、で十分な品質が得られる。
    """
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: dict[str, Any]) -> str:
    """``<content:encoded>`` と ``<description>`` の長い方を本文として採用する。

    一部 WordPress VIP サイト (VB / IEEE Spectrum 等) では片方が truncate
    される運用上の差分があり、長い方を採用するロジックで吸収する
    (`spec collection-source-rss-research.md` の「max(content_encoded, summary)」)。
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
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    feedparser は struct_time を UTC として返す規約 (RFC 2822 の TZ オフセットを
    解釈済みで struct_time に正規化)。``published_parsed`` を優先し、欠損なら
    ``updated_parsed`` で代替する。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


class VentureBeatFetcher:
    """VentureBeat 用 RSS-only Fetcher。"""

    NAME: ClassVar[str] = "VentureBeat"
    ENDPOINT_URL: ClassVar[str] = "https://venturebeat.com/feed"

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "venturebeat_feed_parse_error",
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
        """feed を取得して text を返す。HTTP 系の失敗は Permanent/Temporary に翻訳。"""
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
    ) -> ReadyForArticle | None:
        """1 entry を ``ReadyForArticle`` に変換する純関数 (テスト容易性のため切出)。"""
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
