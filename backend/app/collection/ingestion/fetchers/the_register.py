"""The Register 用 Fetcher — Pattern R+H (Pattern H 設計で実装、Atom feed)。

collection-acquisition-redesign Phase 1d。The Register の Atom フィードは
``<summary>`` に短いリード文しか出さず、本文は HTML を別途取得して
trafilatura で抽出する必要がある (`spec collection-source-rss-research.md`
の Pattern R+H 分類)。

per-source 設計 (実 Atom 観察ベース):

- feed 形式は **Atom (RFC4287)**、``xml:lang="en"``
- ``<link rel="alternate" href>`` は **redirector URL**
  (``https://go.theregister.com/feed/<host>/<path>``)、
  ``_normalize_register_link`` で実 URL に展開してから ``SafeUrl`` 構築する
- ``<id>`` は ``tag:theregister.com,2005:story...`` URI 形式 (NOT redirector)
  → 標準 ``_extract_guid`` でそのまま guid 採用
- ``<author><name>`` を author に採用 (feedparser は ``entry.author`` に
  ``<name>`` のみ抽出、``<email>`` / ``<uri>`` は捨てる)
- ``<category>`` は **未提供** のため tags=() 直書き
- ``<media:>`` namespace 未宣言 → image_url=None 直書き
- ``<title type="html">`` 属性付きだが実観察ではプレーン、defensive で
  ``_strip_html`` を適用
- language は feed-level ``xml:lang="en"`` (NOT en-US)

旧 ``fetchers/rss/the_register.py`` (BaseRssFetcher 継承 + ``convert_entry``
override) は本 PR で削除し、新 Protocol に置き換える。リダイレクタ正規化
ロジックは新 Fetcher に移植 (memory `project_the_register_fetcher_decision.md`
の split case C)。
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
_DEFAULT_LANGUAGE = "en"

_REDIRECTOR_PREFIX = "https://go.theregister.com/feed/"


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (title / author 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Atom の ``<published>`` ISO 8601 (例: ``2026-05-01T21:39:10.00Z``) は
    feedparser が標準解釈する。Pattern H 固有: 本値が None でも Failed 降格
    はしない (HTML 補完を待つ)。
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
    """``<id>`` (feedparser では ``id`` にマップ) を取り出す。

    The Register は Atom ``<id>tag:theregister.com,2005:story...</id>`` を
    提供する (URN tag scheme、redirector ではない実観察)。
    """
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


def _normalize_register_link(raw: str) -> str:
    """``go.theregister.com/feed/<host>/<path>`` → ``https://<host>/<path>`` に直す。

    The Register の Atom フィードは ``<link href>`` がリダイレクタ経由
    (``https://go.theregister.com/feed/www.theregister.com/2026/...``) のため、
    プレフィックスを切り捨てて実 URL を再構築する (memory
    `project_the_register_fetcher_decision.md` の split case C、実観察で 100%
    一貫を確認済)。
    """
    if raw.startswith(_REDIRECTOR_PREFIX):
        return "https://" + raw[len(_REDIRECTOR_PREFIX) :]
    return raw


class TheRegisterFetcher:
    """The Register 用 Pattern H Fetcher (Pattern R+H = HTML 必須、Atom feed)。

    PROVIDES に列挙したフィールドは feed-level / Atom 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``xml:lang="en"``
    - ``guid``: ``<id>tag:theregister.com,2005:story...</id>`` (URN tag scheme)
    - ``site_name``: hardcode "The Register"

    ``author`` (``<author><name>``) は probabilistic のため metadata に詰める
    が PROVIDES には含めない。``tags`` / ``image_url`` は実 Atom で **未提供**
    のため ``()`` / ``None`` を直書きする。
    """

    NAME: ClassVar[str] = "The Register"
    ENDPOINT_URL: ClassVar[str] = "https://www.theregister.com/headlines.atom"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "the_register_feed_parse_error",
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

        固有挙動: ``link`` を ``_normalize_register_link`` で実 URL に展開
        してから ``SafeUrl`` 構築する。
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

        raw_link = entry.get("link", "") or ""
        link = _normalize_register_link(raw_link)
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
            item=PendingHtmlFetch(
                title=title,
                source_id=source_id,
                source_url=source_url,
                published_at_hint=published_at_hint,
            ),
            metadata=metadata,
        )
