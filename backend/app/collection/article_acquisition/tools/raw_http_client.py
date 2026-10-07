"""汎用 raw bytes HTTP 取得 wrapper (sitemap / HTML listing 共有)。"""

from __future__ import annotations

from app.collection.article_acquisition.tools.source_http import get_source_response


class RawHttpClient:
    """raw bytes を取得する thin HTTP client wrapper。"""

    def __init__(self, *, accept: str) -> None:
        self._accept = accept

    async def fetch(self, *, url: str, source_name: str) -> bytes:
        """1 URL を GET し ``bytes`` を返す。

        Raises:
            HttpResponseError / HttpTransportError / HostBlockedError /
                ResponseSizeLimitExceededError: 取得の失敗。
        """
        response = await get_source_response(url, accept=self._accept)
        return response.content
