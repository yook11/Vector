"""TechCrunch 用 Fetcher — Pattern H (RSS で URL 列挙、本文は HTML 必須) の参照実装。

collection-acquisition-redesign Phase 1b'。新 ``Fetcher`` Protocol を満たし、
1 entry ずつ ``IncompleteArticle`` を yield する (品質ゲート未達 entry は
yield しない: Outcome 純化原則)。

TC の RSS feed は ``<description>`` にリード文 (~140 chars) しか含まず、
``<content:encoded>`` も提供しない (`spec collection-source-rss-research.md`)。
このため Fetcher は **本文を取りに行かない** — URL + title を
``IncompleteArticle`` として yield し、後段の ``extract_html_body`` task
が ``ArticleHtmlExtractor`` (trafilatura) で本文を抽出する 2 段構成。

per-source 独立実装 (Pattern H 共通基底は作らない): 「source ごとに取れる
ものが違う」が新 Protocol の設計動機。VB Fetcher と構造は似るが共通基底化
すると差異の表現が逆に難しくなるため、code copy で許容する。
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


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (title clean 用)。

    TC RSS の ``<title>`` は通常タグを含まないが、稀に ``<![CDATA[...]]>``
    に HTML entity (e.g. ``&amp;``) を含むので decode する。本文は HTML
    取得後に trafilatura が処理するため、本関数は title 専用。
    """
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Pattern H 固有: 本値が None でも drop しない (HTML 抽出が
    ``published_at`` を出してくれれば merge 後に最終確定する)。``IncompleteArticle``
    の ``published_at_hint`` に格納される。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


class TechCrunchFetcher:
    """TechCrunch 用 Pattern H Fetcher。"""

    NAME: ClassVar[str] = "TechCrunch"
    ENDPOINT_URL: ClassVar[str] = "https://techcrunch.com/feed/"

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "techcrunch_feed_parse_error",
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
    ) -> IncompleteArticle | None:
        """1 entry を ``IncompleteArticle`` に変換する純関数 (テスト容易性のため切出)。

        Pattern H 固有の品質ゲート (Pattern R より緩い):

        - ``title`` 空 → drop
        - ``link`` 不正 → drop
        - ``published_at`` 欠落 → drop しない (HTML 補完を待つ)
        - ``body`` は本実装では検査しない (Stage 2 = ``extract_html_body`` の責務)
        """
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return None
        title = title[:500]

        link = entry.get("link", "") or ""
        try:
            source_url = SafeUrl(link)
        except ValueError:
            return None

        published_at_hint = _parse_published_at(entry)

        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
        )
