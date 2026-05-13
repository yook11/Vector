"""FierceBiotech 用 Fetcher — Pattern H (RSS で URL 列挙、本文は HTML 必須)。

collection-acquisition-redesign Phase 1c-D。FierceBiotech の RSS は固有の癖
があるため Pattern H 構造同型 PR (1c-C) と切り離して実装する:

per-source 設計 (実 RSS 観察ベース):

- body は **読まない** (Pattern H、Stage 2 = HTML 抽出の責務)
- ``<pubDate>`` が **RFC822 非準拠** ("Apr 30, 2026 6:11pm") のため
  ``feedparser.published_parsed`` が落ちるケースを strptime fallback で救済。
  時刻部 TZ 情報なしのため ET (DST 自動切替) と仮定して UTC 換算する。
- ``<title>`` / ``<dc:creator>`` が HTML 要素 (``<a href="...">``) で wrap
  されているため、両方に ``_strip_html`` を defensive 適用する。
- ``<category>`` / ``<media:>`` namespace が **未提供** のため
  ``tags=()`` / ``image_url=None`` を直書き。
- ``<guid isPermaLink="true">`` (UUID URL 形式) が提供されるため PROVIDES
  に ``guid`` を含める。
- language は ``feed.feed.language`` (= "en", NOT "en-US")。

旧 ``fetchers/rss/fierce_biotech.py`` (BaseRssFetcher 継承の薄スタブ) は本
PR で削除し、新 Protocol に置き換える。
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
_DEFAULT_LANGUAGE = "en"

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
    """HTML タグを剥がして plain text に正規化する (title / author 用)。

    FB RSS の ``<title>`` と ``<dc:creator>`` は ``<a href="...">...</a>``
    で wrap されているため、両方に適用する。本文は HTML 取得後に
    trafilatura が処理するため、本関数は title / author 専用。
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

    Pattern H 固有: 本値が None でも Failed 降格はしない (HTML 抽出が
    ``published_at`` を出してくれれば try_advance_from で merge 後に最終確定)。
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


def _extract_guid(entry: dict[str, Any]) -> str | None:
    """``<guid>`` (feedparser では ``id`` にマップ) を取り出す。

    FB は ``<guid isPermaLink="true">`` で UUID URL 形式
    ("https://www.fiercebiotech.com/<uuid>") を提供する (実観察)。
    """
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    """``en_US`` / ``en-us`` / ``en-US`` の表記揺れを統一。FB は "en" passthrough。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class FierceBiotechFetcher:
    """FierceBiotech 用 Pattern H Fetcher。

    PROVIDES に列挙したフィールドは feed-level / RSS 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``<channel><language>`` ("en")
    - ``guid``: ``<guid isPermaLink="true">`` (UUID URL)
    - ``site_name``: hardcode "FierceBiotech"

    ``author`` は probabilistic (HTML wrap で形式変動の余地あり) なため
    metadata に詰めるが PROVIDES には含めない。``tags`` / ``image_url`` は
    実 RSS で **未提供** のため ``()`` / ``None`` を直書きする。
    """

    NAME: ClassVar[str] = "FierceBiotech"
    ENDPOINT_URL: ClassVar[str] = "https://www.fiercebiotech.com/rss/xml"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
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

        raw_author = entry.get("author")
        if isinstance(raw_author, str) and raw_author:
            author = _strip_html(raw_author)[:200] or None
        else:
            author = None

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
        }
        if author:
            metadata["author"] = author
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
