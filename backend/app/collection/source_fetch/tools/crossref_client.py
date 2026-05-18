"""Crossref Works API thin client wrapper。

P5 で MDPI 4 journal の Crossref API 経路 Adapter を ``SourceAdapter`` 化する
に際し、Crossref REST API の HTTP 取得 + JSON decode + per-ISSN filter +
sort/order 構築を集約する責務切り出し。

設計判断:

- ``works(*, issn, from_pub_date, rows)`` で呼び出し側は意味だけ渡し、
  ``filter=issn:..,from-pub-date:..`` / ``sort=published`` / ``order=desc``
  の構築は wrapper 内で完結 (旧 ``BaseMDPICrossrefFetcher._fetch_recent_works``
  ``mdpi/_common.py:167`` と同 params 契約を継承)。
- polite pool 降格防止のため User-Agent に ``mailto:`` を必須で乗せる。
- ``list[dict]`` を返すだけ。type filter / license gate / JATS strip 等の
  業務判定は Adapter の責務。
- test では本 client を継承した fixture-backed fake を Adapter に DI する。
"""

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
    """Crossref Works API thin wrapper。

    per-ISSN filter + 公開日 rolling window で取得し、``items: list[dict]`` を
    返す。caller (Adapter) は各 item の type/license/title/abstract/date/DOI
    判定を担う (旧 ``BaseMDPICrossrefFetcher._convert_record`` と同等の責務分担)。
    """

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
        """per-ISSN + ``from-pub-date`` で recent works を取得。

        ``sort=published`` / ``order=desc`` を継承して新着優先を契約として保つ
        (旧 ``mdpi/_common.py:167`` と同値)。

        Raises:
            ExternalFetchError: HTTP status / transport / SSRF 例外を
                ``translate_fetch_exception`` で写像した origin error。
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
