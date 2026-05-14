"""FierceBiotech 用 Fetcher — Pattern H (RSS で URL 列挙、本文は HTML 必須)。

collection-acquisition-redesign Phase 1c-D。FierceBiotech の RSS は固有の癖
があるため Pattern H 構造同型 PR (1c-C) と切り離して実装する:

per-source 設計 (実 RSS 観察ベース):

- body は **読まない** (Pattern H、Stage 2 = HTML 抽出の責務)
- ``<pubDate>`` が **RFC822 非準拠** ("Apr 30, 2026 6:11pm") のため
  ``feedparser.published_parsed`` が落ちるケースを strptime fallback で救済。
  時刻部 TZ 情報なしのため ET (DST 自動切替) と仮定して UTC 換算する。
- ``<title>`` が HTML 要素 (``<a href="...">``) で wrap されているため
  ``_strip_html`` を defensive 適用する。
- language は ``feed.feed.language`` (= "en", NOT "en-US")。
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

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

_FB_PUBDATE_FORMAT = "%b %d, %Y %I:%M%p"
"""FierceBiotech 固有の pubDate format ("Apr 30, 2026 6:11pm")。

%b = 月名 3 文字 / %d = 日 / %Y = 4 桁年 / %I = 12 時間制 (非ゼロ埋め可) /
%M = 分 / %p = AM/PM (Linux glibc では am/pm/AM/PM すべて受理)。

実観察: "Apr 30, 2026 6:11pm" / "Apr 30, 2026 1:18pm" — 時刻部は非ゼロ埋め。
"""

_FB_TZ = ZoneInfo("America/New_York")
"""FierceBiotech の TZ 仮定 (Fierce Network = US biotech、東海岸)。

RSS には TZ 情報が含まれないため、ローカル発信時刻と推定して ET (DST 自動
切替) を適用する。本仮定は ±1 時間程度の誤差を許容する設計判断 (Stage 2 /
Stage 3 での ranking や digest week 算出に微影響あり)。
"""


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (title 用)。

    FB RSS の ``<title>`` は ``<a href="...">...</a>`` で wrap されているため
    適用する。本文は HTML 取得後に trafilatura が処理するため、本関数は title
    専用。
    """
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """FB 固有: feedparser が non-RFC822 を解釈できないため strptime fallback。

    優先順位:

    1. ``feedparser.published_parsed`` / ``updated_parsed`` (struct_time、
       将来 RFC822 化された場合に自動吸収)
    2. ``entry.published`` / ``entry.updated`` (生文字列) を
       ``%b %d, %Y %I:%M%p`` で解釈し、ET TZ を付与してから UTC 変換

    Pattern H 固有: 本値が None でも drop しない (HTML 抽出が
    ``published_at`` を出してくれれば complete_with_html で merge 後に最終確定)。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is not None:
        try:
            return PublishedAt(value=datetime(*parsed[:6], tzinfo=UTC))
        except (TypeError, ValueError):
            pass

    raw = entry.get("published") or entry.get("updated")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.strptime(raw.strip(), _FB_PUBDATE_FORMAT)  # noqa: DTZ007
    except ValueError:
        return None
    return PublishedAt(value=dt.replace(tzinfo=_FB_TZ).astimezone(UTC))


class FierceBiotechFetcher:
    """FierceBiotech 用 Pattern H Fetcher。"""

    NAME: ClassVar[str] = "FierceBiotech"
    ENDPOINT_URL: ClassVar[str] = "https://www.fiercebiotech.com/rss/xml"

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "fierce_biotech_feed_parse_error",
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
