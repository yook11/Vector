"""Crossref Works API の thin client。"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import structlog

from app.collection.source_fetch.tools.http_error_translation import (
    translate_fetch_exception,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)

# Crossref polite pool 降格防止のため User-Agent に mailto: が必須。
_USER_AGENT = (
    "Mozilla/5.0 (compatible; Vector/1.0; "
    "+https://github.com/yook11/Vector; mailto:crossref-contact@example.invalid)"
)
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class CrossrefApiClient:
    """Crossref Works API thin wrapper。"""

    DEFAULT_ENDPOINT: ClassVar[str] = "https://api.crossref.org/works"

    def __init__(self, *, endpoint_url: str = DEFAULT_ENDPOINT) -> None:
        self._endpoint_url = endpoint_url

    async def works(
        self,
        *,
        source_name: str,
        issn: str,
        from_pub_date: str,
        rows: int,
    ) -> list[dict[str, Any]]:
        """per-ISSN + ``from-pub-date`` で新着順に recent works を取得。

        Raises:
            ExternalFetchError: HTTP status / transport / SSRF 例外の写像。
        """
        params: dict[str, str | int] = {
            "filter": f"issn:{issn},from-pub-date:{from_pub_date}",
            "rows": rows,
            "sort": "published",
            "order": "desc",
        }

        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
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

        items: list[dict[str, Any]] = list(data.get("message", {}).get("items", []))
        if not items:
            logger.info("crossref_no_new_items", source=source_name)
        return items
