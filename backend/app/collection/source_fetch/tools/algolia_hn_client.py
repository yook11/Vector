"""Hacker News Algolia Search API の thin client。"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import httpx
import structlog

from app.collection.source_fetch.tools.http_error_translation import (
    translate_fetch_exception,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class HackerNewsApiClient:
    """Algolia HN Search API thin wrapper。"""

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

            data = response.json()

        hits: list[dict[str, Any]] = list(data.get("hits", []))
        if not hits:
            logger.info("hn_no_new_stories", source=source_name)
        return hits
