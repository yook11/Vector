"""Hacker News Algolia Search API の Reader (HTTP 取得 + hit→Entry 抽出)。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

import httpx
import structlog

from app.collection.article_collection.errors import UnreadableResponseError
from app.collection.article_collection.tools.http_error_translation import (
    translate_fetch_exception,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


@dataclass(frozen=True, slots=True)
class HackerNewsEntry:
    """Algolia HN Search API の 1 hit を写した Entry。``published`` は
    ``created_at`` を写した tz-aware datetime か ``None``。"""

    url: str | None
    title: str | None
    published: datetime | None
    raw_created_at: str | None


def _parse_created_at(raw: str | None) -> datetime | None:
    """Algolia の ``created_at`` (ISO 8601 + ``Z``) を tz-aware UTC datetime に。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def normalize_hit(hit: dict[str, Any]) -> HackerNewsEntry:
    """1 Algolia hit を ``HackerNewsEntry`` に写す。"""
    raw_created = hit.get("created_at")
    raw_created = raw_created if isinstance(raw_created, str) else None
    return HackerNewsEntry(
        url=hit.get("url"),
        title=hit.get("title"),
        published=_parse_created_at(raw_created),
        raw_created_at=raw_created,
    )


class HackerNewsReader:
    """Algolia HN Search API Reader。"""

    DEFAULT_ENDPOINT: ClassVar[str] = "https://hn.algolia.com/api/v1/search_by_date"

    def __init__(self, *, endpoint_url: str = DEFAULT_ENDPOINT) -> None:
        self._endpoint_url = endpoint_url

    async def search_recent_stories(
        self,
        *,
        source_name: str,
        min_points: int,
        window_seconds: int,
        hits_per_page: int,
    ) -> list[HackerNewsEntry]:
        """直近 ``window_seconds`` 内に投稿された ``points > min_points`` story を取得。

        Raises:
            ExternalFetchError: HTTP status / transport / SSRF 例外の写像。
        """
        since = int(time.time()) - window_seconds
        params: dict[str, str | int] = {
            "tags": "story",
            "hitsPerPage": hits_per_page,
            "numericFilters": f"points>{min_points},created_at_i>{since}",
        }

        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(self._endpoint_url, params=params)
                response.raise_for_status()
            except (
                httpx.HTTPStatusError,
                httpx.RequestError,
                HostBlockedError,
                HostResolutionError,
            ) as e:
                raise translate_fetch_exception(e, source_name=source_name) from e

            try:
                data = response.json()
            except json.JSONDecodeError as e:
                raise UnreadableResponseError(
                    f"hn json decode error: {source_name}: {e}"
                ) from e

        # envelope shape を確定してから抽出 (接続成功でも構造化できなければ
        # read 失敗。absent key は寛容に空へ、present だが型違いは unreadable)。
        if not isinstance(data, dict):
            raise UnreadableResponseError(f"hn envelope shape error: {source_name}")
        hits_raw = data.get("hits", [])
        if not isinstance(hits_raw, list):
            raise UnreadableResponseError(f"hn envelope shape error: {source_name}")
        hits: list[dict[str, Any]] = hits_raw
        if not hits:
            logger.info("hn_no_new_stories", source=source_name)
        return [normalize_hit(hit) for hit in hits]
