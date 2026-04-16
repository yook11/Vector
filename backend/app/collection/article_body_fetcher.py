"""記事本文フェッチャ — URL から記事本文を取得する。

単一責務のクラス: URL を受け取り、本文テキストを返すか、品質ゲートで
弾かれた場合は ``None`` を返す。恒久的な失敗と一時的な失敗は例外として
分離し、呼び出し側でビジネス判断とリトライ判断を切り分けられるようにする。

内部実装（``RobotsCache``、``httpx`` クライアントのライフサイクル、
``trafilatura`` パーサ）はここで隠蔽され、呼び出し側は
``URL -> 本文 | None`` の契約にのみ依存する。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
import trafilatura

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
USER_AGENT = "VectorBot/1.0 (+https://github.com/vector-news)"
HEADERS = {"User-Agent": USER_AGENT}


class PermanentFetchError(Exception):
    """リトライ不可のフェッチ失敗（403 / 404 / robots.txt で拒否）。"""


class TemporaryFetchError(Exception):
    """リトライ可能なフェッチ失敗（5xx / タイムアウト / 429）。"""


class _RobotsCache:
    """並行アクセス対応の robots.txt キャッシュ。

    同じドメインを複数コルーチンが同時参照した際の重複フェッチを防ぐため、
    ドメイン単位の ``asyncio.Lock`` を用いる。
    """

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check(self, client: httpx.AsyncClient, url: str) -> bool:
        """URL が robots.txt で許可されているか判定する。許可なら True。"""
        parsed = urlparse(url)
        domain = parsed.netloc
        robots_url = f"{parsed.scheme}://{domain}/robots.txt"

        async with self._locks[domain]:
            if domain in self._cache:
                rp = self._cache[domain]
                return rp is None or rp.can_fetch(USER_AGENT, url)

            # 初回アクセス: robots.txt を取得してキャッシュする
            try:
                resp = await client.get(robots_url, timeout=10.0)
                if resp.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(resp.text.splitlines())
                    self._cache[domain] = rp
                else:
                    # robots.txt が存在しない/エラー → 許可とみなす
                    self._cache[domain] = None
            except httpx.HTTPError:
                self._cache[domain] = None

            rp = self._cache[domain]
            return rp is None or rp.can_fetch(USER_AGENT, url)


def _parse_article_html(html: str, url: str) -> str | None:
    """trafilatura で HTML から記事本文を抽出する（同期・CPU バウンド）。

    本関数は ``asyncio.to_thread()`` 経由で呼ぶことを想定している。
    """
    text = trafilatura.extract(
        html,
        url=url,
        favor_precision=True,
        include_comments=False,
        include_tables=True,
        deduplicate=True,
    )
    if not text or len(text.strip()) < 50:
        return None
    return text.strip()


class ArticleBodyFetcher:
    """URL から記事本文テキストを取得するフェッチャ。

    呼び出し側は ``fetch(url) -> str | None`` の契約のみに依存する。
    robots キャッシュや HTTP クライアントのライフサイクルは内部で完結する。
    """

    def __init__(self) -> None:
        self._robots_cache = _RobotsCache()

    async def fetch(self, url: str) -> str | None:
        """指定 URL の記事本文を取得する。

        Returns:
            str: 抽出した記事本文テキスト。
            None: Content-Type が不一致、または品質ゲートで棄却（恒久的）。

        Raises:
            PermanentFetchError: robots.txt 拒否 / 403 / 404 / 410 / 451。
            TemporaryFetchError: 5xx / 429 / タイムアウト / ネットワークエラー。
        """
        async with httpx.AsyncClient(headers=HEADERS, timeout=HTTP_TIMEOUT) as client:
            if not await self._robots_cache.check(client, url):
                raise PermanentFetchError(f"robots.txt blocked: {url}")

            try:
                response = await client.get(
                    url, timeout=HTTP_TIMEOUT, follow_redirects=True
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {url}") from e
                # 429 / 5xx はリトライ可能
                raise TemporaryFetchError(f"HTTP {status}: {url}") from e
            except httpx.RequestError as e:
                # タイムアウト / DNS / 接続エラーはリトライ可能
                raise TemporaryFetchError(f"request error: {url}: {e}") from e

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                logger.info("content_not_html", url=url, content_type=content_type)
                return None

            try:
                text = await asyncio.to_thread(_parse_article_html, response.text, url)
            except Exception as e:
                logger.warning("content_parse_error", url=url, error=str(e))
                return None

            return text
