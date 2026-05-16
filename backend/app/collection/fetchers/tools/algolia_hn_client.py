"""Hacker News Algolia Search API thin client wrapper。

P5 で ``HackerNewsAdapter`` を ``SourceAdapter`` 化するに際し、Algolia HN
Search API の HTTP 取得 + JSON decode + ``points>N`` / ``created_at_i>since``
の numericFilters 構築を集約する責務切り出し。

設計判断:

- ``search_recent_stories(*, min_points, window_seconds, hits_per_page)`` で
  呼び出し側は意味だけ渡し、サーバサイド filter 構築は wrapper 内で完結。
- HTTP / transport / SSRF 例外は ``translate_fetch_exception`` 経由で origin
  ``ExternalFetchError`` に写像 (``RawHttpClient`` と相同)。
- ``list[dict]`` を返すだけ。各 hit の意味付け (``url=None`` skip 等) は
  Adapter の責務。
- test では本 client を継承した fixture-backed fake を Adapter に DI する。
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import httpx
import structlog

from app.collection.fetchers.tools.http_error_translation import (
    translate_fetch_exception,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class HackerNewsApiClient:
    """Algolia HN Search API thin wrapper。

    sliding window + 最低 points + story tag を全部 server-side で絞り込み、
    ``hits: list[dict]`` を返す。caller (Adapter) は hit の意味付けだけを担う。
    """

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
    ) -> list[dict[str, Any]]:
        """直近 ``window_seconds`` 内に投稿された ``points > min_points`` story を取得。

        Raises:
            ExternalFetchError: HTTP status / transport / SSRF 例外を
                ``translate_fetch_exception`` で写像した origin error。
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

            data = response.json()

        hits: list[dict[str, Any]] = list(data.get("hits", []))
        if not hits:
            logger.info("hn_no_new_stories", source=source_name)
        return hits
