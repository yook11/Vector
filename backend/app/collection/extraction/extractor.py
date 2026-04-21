"""HTML 抽出層 — URL から記事本文と公開日時を取得する。

単一責務のクラス: URL を受け取り、HTML から本文テキストと公開日時を
抽出して返す。恒久的な失敗と一時的な失敗は例外として分離し、
呼び出し側でビジネス判断とリトライ判断を切り分けられるようにする。

内部実装（``RobotsCache``、``httpx`` クライアントのライフサイクル、
``trafilatura`` パーサ）はここで隠蔽され、呼び出し側は
``URL -> HtmlExtractionResult`` の契約にのみ依存する。
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
import trafilatura

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.candidate import PublishedAt
from app.domain.safe_url import SafeUrl
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
_TITLE_MAX_LENGTH = 500
USER_AGENT = "VectorBot/1.0 (+https://github.com/vector-news)"
HEADERS = {"User-Agent": USER_AGENT}

# HTML meta charset を検出する正規表現（先頭バイト列から探す）
_META_CHARSET_RE = re.compile(
    rb'<meta\s+charset\s*=\s*["\']?\s*([^"\'\s;>]+)', re.IGNORECASE
)
_META_HTTP_EQUIV_CHARSET_RE = re.compile(rb"charset\s*=\s*([^\s\"';>]+)", re.IGNORECASE)
_SNIFF_BYTES = 2048


def _decode_html_response(response: httpx.Response) -> str:
    """HTTP レスポンスの HTML を正しいエンコーディングでデコードする。

    httpx はHTTP Content-Type ヘッダーの charset を優先するが、
    charset が明示されていない場合は UTF-8 にフォールバックする。
    日本語サイト（IT media 等）では HTTP ヘッダーに charset がなく
    HTML の ``<meta charset="Shift_JIS">`` でのみ宣言されるケースがあり、
    その場合 httpx のデフォルト UTF-8 デコードで文字化けが発生する。

    本関数は Content-Type に charset がない場合のみ HTML meta charset を
    スニッフィングし、正しいエンコーディングでデコードする。
    """
    # Content-Type ヘッダーに charset があれば httpx のデコードを信頼する
    if response.charset_encoding is not None:
        return response.text

    # HTML 先頭バイトから meta charset を探す
    raw = response.content
    head = raw[:_SNIFF_BYTES]

    match = _META_CHARSET_RE.search(head) or _META_HTTP_EQUIV_CHARSET_RE.search(head)
    if match:
        encoding = match.group(1).decode("ascii", errors="ignore").strip()
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            logger.warning(
                "html_charset_decode_failed",
                declared_charset=encoding,
                url=str(response.url),
            )

    # meta charset もなければ httpx のデフォルト（UTF-8）にフォールバック
    return response.text


@dataclass(frozen=True)
class HtmlExtractionResult:
    """HTML 抽出結果。本文・タイトル・公開日時をそれぞれ独立に保持する。"""

    body: str | None
    title: str | None
    published_at: datetime | None


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


def _extract_from_html(html: str, url: str) -> HtmlExtractionResult:
    """trafilatura で HTML から記事本文と公開日時を抽出する（同期・CPU バウンド）。

    本関数は ``asyncio.to_thread()`` 経由で呼ぶことを想定している。
    """
    result = trafilatura.bare_extraction(
        html,
        url=url,
        favor_precision=True,
        include_comments=False,
        include_tables=True,
        deduplicate=True,
        with_metadata=True,
        date_extraction_params={
            "original_date": True,
            "extensive_search": True,
            "max_date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "outputformat": "%Y-%m-%dT%H:%M:%S",
        },
    )

    if result is None:
        return HtmlExtractionResult(body=None, title=None, published_at=None)

    # 本文の品質ゲート: 50 文字未満は棄却
    text = result.get("text")
    body = text.strip() if text and len(text.strip()) >= 50 else None

    # タイトル: trafilatura が OGP / Twitter Card / JSON-LD / <title> / h1 の順で抽出。
    # HTML タグ除去と 500 文字上限で整形し、空なら None。
    cleaned_title = strip_html_tags(result.get("title"))
    title = cleaned_title[:_TITLE_MAX_LENGTH] if cleaned_title else None

    parsed_published = PublishedAt.parse(result.get("date"))
    published_at = parsed_published.value if parsed_published is not None else None

    return HtmlExtractionResult(body=body, title=title, published_at=published_at)


class ArticleHtmlExtractor:
    """URL から記事本文と公開日時を取得する抽出器。

    呼び出し側は ``fetch(url) -> HtmlExtractionResult`` の契約のみに依存する。
    robots キャッシュや HTTP クライアントのライフサイクルは内部で完結する。
    """

    def __init__(self) -> None:
        self._robots_cache = _RobotsCache()

    async def fetch(self, url: SafeUrl) -> HtmlExtractionResult:
        """指定 URL の HTML から記事本文・タイトル・公開日時を抽出する。

        Returns:
            HtmlExtractionResult: body / title / published_at を独立に保持。
            Content-Type が不一致の場合は全て None。

        Raises:
            PermanentFetchError: robots.txt 拒否 / 403 / 404 / 410 / 451。
            TemporaryFetchError: 5xx / 429 / タイムアウト / ネットワークエラー。
        """
        url_str = str(url)
        async with httpx.AsyncClient(headers=HEADERS, timeout=HTTP_TIMEOUT) as client:
            if not await self._robots_cache.check(client, url_str):
                raise PermanentFetchError(f"robots.txt blocked: {url_str}")

            try:
                response = await client.get(
                    url_str, timeout=HTTP_TIMEOUT, follow_redirects=True
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {url_str}") from e
                # 429 / 5xx はリトライ可能
                raise TemporaryFetchError(f"HTTP {status}: {url_str}") from e
            except httpx.RequestError as e:
                # タイムアウト / DNS / 接続エラーはリトライ可能
                raise TemporaryFetchError(f"request error: {url_str}: {e}") from e

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                logger.info("content_not_html", url=url_str, content_type=content_type)
                return HtmlExtractionResult(body=None, title=None, published_at=None)

            try:
                html_text = _decode_html_response(response)
                result = await asyncio.to_thread(_extract_from_html, html_text, url_str)
            except Exception as e:
                logger.warning("content_parse_error", url=url_str, error=str(e))
                return HtmlExtractionResult(body=None, title=None, published_at=None)

            return result
