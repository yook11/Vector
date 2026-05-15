"""汎用 raw bytes HTTP 取得 wrapper (sitemap / HTML listing 共有)。

P5 で Pattern H 系の sitemap.xml / HTML listing Adapter (Anthropic / ORNL) を
``SourceAdapter`` 化するに際し、HTTP 取得 + SSRF guard + HTTP error 分類を
本モジュールに集約する責務切り出し。

設計判断:

- SSRF guard ・HTTP error → ``Permanent``/``Temporary`` 分類はここで完結。
  Adapter の ``collect()`` 本体には ``try/except httpx.*`` を書かない
  (構造的に握り潰しが起きない設計)。
- parse は呼び出し側 (Adapter) の責務。本 wrapper は ``bytes`` を返すだけ。
- test では ``RawHttpClient`` を継承した fixture-backed fake を Adapter に
  コンストラクタ DI で差し込む。本物の ``fetch`` は呼ばれないため
  network I/O は完全に排除できる (P4 RSS の ``RssParser`` DI と相同)。
"""

from __future__ import annotations

from typing import ClassVar

import httpx

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
)
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class RawHttpClient:
    """raw bytes を取得する thin HTTP client wrapper。

    ``Accept`` ヘッダのみ source 種別 (sitemap.xml = ``application/xml``、
    HTML listing = ``text/html``) で切替える。timeout は Crawl-delay 10s
    対応で十分長めに取った既存値を共有する。
    """

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
            PermanentFetchError: 403 / 404 / 410 / 451 / SSRF host 拒否。
            TemporaryFetchError: 429 / 5xx / タイムアウト / DNS 一時失敗。
        """
        async with make_safe_async_client(
            headers={"User-Agent": self._user_agent, "Accept": self._accept},
            verify=True,
            timeout=self._timeout,
        ) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {source_name}") from e
                raise TemporaryFetchError(f"HTTP {status}: {source_name}") from e
            except httpx.RequestError as e:
                raise TemporaryFetchError(f"request error: {source_name}: {e}") from e
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e
            return response.content
