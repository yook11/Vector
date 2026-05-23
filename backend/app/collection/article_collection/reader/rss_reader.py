"""RSS / Atom / RDF feed の取得 + 正規化道具。

HTTP 取得・SSRF guard・feedparser 呼び出し・error 翻訳・title 平文化を担い、
正規化済の ``RssEntry`` を返す。body 系 (``summary`` / ``content_encoded``) は
raw HTML のまま返し、body picker / footer 除去等は呼び出し側の責務。
Shift_JIS など XML 宣言で encoding を持つ feed は ``parse_mode="bytes"`` を選び
feedparser に sniff を委ねる。bozo かつ entries 空は ``FetchParseError``。
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time
from typing import Any, Literal

import feedparser
import httpx
import structlog

from app.collection.article_collection.tools.http_error_translation import (
    translate_fetch_exception,
)
from app.collection.external_fetch_errors import FetchParseError
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)

ParseMode = Literal["text", "bytes"]

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
)
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class RssEntry:
    """正規化された RSS / Atom / RDF entry。

    title は平文化済。body 系は raw HTML を保持する。
    """

    link: str
    title: str
    guid: str | None
    published: datetime | None
    summary: str | None
    content_encoded: str | None
    tags: tuple[str, ...]
    raw_published: str | None
    raw_updated: str | None


def _strip_html_to_plain(s: str) -> str:
    """HTML tag 除去 + entity decode + 空白圧縮。title 正規化用。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _to_utc(parsed: struct_time | None) -> datetime | None:
    """feedparser の ``*_parsed`` (GMT struct_time) を UTC datetime に変換。"""
    if parsed is None:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _extract_content_encoded(entry: dict[str, Any]) -> str | None:
    """``<content:encoded>`` (Atom ``<content>``) の最初の ``value`` を返す。"""
    content = entry.get("content")
    if not isinstance(content, list) or not content:
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if isinstance(value, str) and value:
            return value
    return None


def _extract_tags(entry: dict[str, Any]) -> tuple[str, ...]:
    """``<category>`` (feedparser ``tags``) の ``term`` を tuple で返す。

    feedparser は ``entry.tags`` を ``[{term, scheme, label}, ...]`` 形式に
    マップする。``term`` が文字列のものだけを採用する。
    """
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return ()
    result: list[str] = []
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        term = tag.get("term")
        if isinstance(term, str) and term:
            result.append(term)
    return tuple(result)


def normalize_entry(entry: dict[str, Any]) -> RssEntry:
    """feedparser dict を ``RssEntry`` に正規化する。"""
    raw_link = entry.get("link", "") or ""
    raw_title = entry.get("title", "") or ""
    plain_title = _strip_html_to_plain(raw_title)

    raw_guid = entry.get("id") or entry.get("guid")
    guid: str | None
    if isinstance(raw_guid, str) and raw_guid:
        guid = raw_guid[:2048]
    else:
        guid = None

    published = _to_utc(entry.get("published_parsed") or entry.get("updated_parsed"))

    summary_value = entry.get("summary")
    summary = (
        summary_value if isinstance(summary_value, str) and summary_value else None
    )

    raw_published_value = entry.get("published")
    raw_published = (
        raw_published_value
        if isinstance(raw_published_value, str) and raw_published_value
        else None
    )
    raw_updated_value = entry.get("updated")
    raw_updated = (
        raw_updated_value
        if isinstance(raw_updated_value, str) and raw_updated_value
        else None
    )

    return RssEntry(
        link=raw_link,
        title=plain_title,
        guid=guid,
        published=published,
        summary=summary,
        content_encoded=_extract_content_encoded(entry),
        tags=_extract_tags(entry),
        raw_published=raw_published,
        raw_updated=raw_updated,
    )


class RssReader:
    """HTTP + feedparser parse + 正規化を行う無状態クライアント。

    SSRF guard 入り client を内部で組み立てる (外部注入不可)。
    """

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: ParseMode = "text",
        user_agent: str = _DEFAULT_USER_AGENT,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> list[RssEntry]:
        """HTTP GET → feedparser → ``list[RssEntry]`` まで完結する。

        Raises:
            FetchParseError: bozo かつ entries 空 (feed 構造破損)。
            ExternalFetchError: HTTP status / transport / SSRF 例外の写像。
        """
        raw = await self._fetch_raw(
            endpoint_url=endpoint_url,
            source_name=source_name,
            parse_mode=parse_mode,
            user_agent=user_agent,
            timeout=timeout,
        )
        feed = await asyncio.to_thread(feedparser.parse, raw)
        if feed.bozo and not feed.entries:
            logger.warning(
                "rss_feed_parse_error",
                source=source_name,
                error=str(feed.bozo_exception),
            )
            raise FetchParseError(
                f"feed parse error: {source_name}: {feed.bozo_exception}"
            )
        return [normalize_entry(entry) for entry in feed.entries]

    async def _fetch_raw(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: ParseMode,
        user_agent: str,
        timeout: httpx.Timeout,
    ) -> str | bytes:
        async with make_safe_async_client(
            headers={"User-Agent": user_agent},
            verify=True,
            timeout=timeout,
        ) as client:
            try:
                response = await client.get(endpoint_url)
                response.raise_for_status()
            except (
                httpx.HTTPStatusError,
                httpx.RequestError,
                HostBlockedError,
                HostResolutionError,
            ) as e:
                raise translate_fetch_exception(e, source_name=source_name) from e
            return response.content if parse_mode == "bytes" else response.text
