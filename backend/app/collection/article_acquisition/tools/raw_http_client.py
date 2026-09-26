"""汎用 raw bytes HTTP 取得 wrapper (sitemap / HTML listing 共有)。"""

from __future__ import annotations

from typing import ClassVar

import httpx

from app.collection.article_acquisition.tools.source_http import get_source_response
from app.http.external import make_external_async_client

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
)
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class RawHttpClient:
    """raw bytes を取得する thin HTTP client wrapper。"""

    DEFAULT_USER_AGENT: ClassVar[str] = _DEFAULT_USER_AGENT

    def __init__(
        self,
        *,
        accept: str,
        user_agent: str = _DEFAULT_USER_AGENT,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        self._accept = accept
        self._user_agent = user_agent
        self._timeout = timeout

    async def fetch(self, *, url: str, source_name: str) -> bytes:
        """1 URL を GET し ``bytes`` を返す。

        Raises:
            HttpResponseError / HttpTransportError / HostBlockedError: 取得の失敗。
        """
        async with make_external_async_client(
            headers={"User-Agent": self._user_agent, "Accept": self._accept},
            verify=True,
            timeout=self._timeout,
        ) as client:
            response = await get_source_response(client, url)
            return response.content
