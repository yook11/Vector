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
from typing import Literal
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
import trafilatura

from app.collection.article.domain.article import (
    _ARTICLE_BODY_MIN_LENGTH as _BODY_MIN_LENGTH,
)
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.external_fetch_errors import (
    FetchRedirectBlockedError,
    FetchResponseTooLargeError,
    FetchRobotsDisallowedError,
)
from app.collection.fetchers.tools.http_error_translation import (
    translate_fetch_exception,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
_TITLE_MAX_LENGTH = 500
# 1 記事あたりの HTTP レスポンス本体の上限 (10 MiB)。
# CONTENT_MAX_LENGTH は抽出後の文字数上限なので別関心事。
# ここではフェッチ層で「内部の大きなレスポンスを引き出される」攻撃面を
# 構造的に閉じる (defense-in-depth)。
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
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


ExtractionEmptyReason = Literal["not_html", "parse_error", "quality_gate"]


@dataclass(frozen=True)
class ExtractedContent:
    """抽出成功: 品質ゲートを通過した本文・タイトル。

    invariant:
      - ``title``: 非空、500 文字以内
      - ``body``: 50 文字以上
      - ``published_at``: 任意（記事によっては取得不能で妥当）
    """

    title: str
    body: str
    published_at: PublishedAt | None

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("title must be non-empty")
        if len(self.title) > _TITLE_MAX_LENGTH:
            raise ValueError(f"title exceeds {_TITLE_MAX_LENGTH} chars")
        if len(self.body) < _BODY_MIN_LENGTH:
            raise ValueError(f"body must be at least {_BODY_MIN_LENGTH} chars")


@dataclass(frozen=True)
class ExtractionEmpty:
    """抽出不能: Content-Type 不一致 / パース失敗 / 品質ゲート未達。

    ``reason`` は観測性のためだけに保持する（現状の呼び出し側は
    全ケースを同一に扱うが、メトリクスとログに理由を載せる）。
    """

    reason: ExtractionEmptyReason


HtmlExtractionResult = ExtractedContent | ExtractionEmpty


class _RobotsCache:
    """並行アクセス対応の robots.txt キャッシュ。

    同じドメインを複数コルーチンが同時参照した際の重複フェッチを防ぐため、
    ドメイン単位の ``asyncio.Lock`` を用いる。
    """

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check(self, client: httpx.AsyncClient, url: str) -> bool:  # noqa: TID251
        """URL が robots.txt で許可されているか判定する。許可なら True。

        ``client`` は ``make_safe_async_client`` 経由で構築されており、event_hook
        により本メソッド内の ``client.get`` でも SSRF 検証が走る (Vuln 5)。
        """
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
        return ExtractionEmpty(reason="parse_error")

    # trafilatura 2.0 以降、bare_extraction() は Document インスタンスを返す
    # 本文の品質ゲート: 50 文字未満は棄却
    text = result.text
    body = text.strip() if text and len(text.strip()) >= _BODY_MIN_LENGTH else None

    # タイトル: trafilatura が OGP / Twitter Card / JSON-LD / <title> / h1 の順で抽出。
    # HTML タグ除去と 500 文字上限で整形し、空なら None。
    cleaned_title = strip_html_tags(result.title)
    title = cleaned_title[:_TITLE_MAX_LENGTH] if cleaned_title else None

    if body is None or title is None:
        return ExtractionEmpty(reason="quality_gate")

    return ExtractedContent(
        title=title,
        body=body,
        published_at=PublishedAt.parse(result.date),
    )


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
            HtmlExtractionResult: ``ExtractedContent``（成功）または
            ``ExtractionEmpty``（Content-Type 不一致 / パース失敗 / 品質ゲート未達）。

        Raises:
            ExternalFetchError: robots Disallow / redirect block / response 過大 /
            HTTP status (4xx/5xx) / transport (timeout/network) / SSRF block。
            どの origin failure かは subclass で表現し、retry / terminal の
            判断は Stage 2 の disposition mapper が行う (本層は分類しない)。
        """
        url_str = str(url)

        # SSRF defense は make_safe_async_client の event_hook に集約済み。
        # 全 client.get 直前で ensure_host_is_public が走り、政策層の例外
        # (HostBlockedError / HostResolutionError) が伝播する。HTTP status /
        # transport / SSRF の origin error 翻訳は translate_fetch_exception
        # (SSoT) に一本化し、本層で status_code を直書き分類しない。
        async with make_safe_async_client(
            headers=HEADERS, timeout=HTTP_TIMEOUT
        ) as client:
            try:
                if not await self._robots_cache.check(client, url_str):
                    raise FetchRobotsDisallowedError(f"robots.txt blocked: {url_str}")

                response = await client.get(url_str, timeout=HTTP_TIMEOUT)
                # 3xx は raise_for_status では拾われない: 明示的に弾く。
                # follow_redirects=False (make_safe_async_client の default) は
                # Location 経由 SSRF を遮断する。
                if 300 <= response.status_code < 400:
                    logger.info(
                        "redirect_not_followed",
                        url=url_str,
                        status=response.status_code,
                        location=response.headers.get("location", "")[:200],
                    )
                    raise FetchRedirectBlockedError(
                        f"redirect not followed: HTTP {response.status_code}: {url_str}"
                    )
                response.raise_for_status()
            except (httpx.HTTPError, HostBlockedError, HostResolutionError) as e:
                raise translate_fetch_exception(e, source_name=url_str) from e

            # レスポンスサイズ上限: 内部エンドポイントから巨大レスポンスを
            # 引き出される攻撃面を閉じる (Content-Length 自己申告 + 実バイト数)。
            content_length_header = response.headers.get("content-length")
            if content_length_header is not None:
                try:
                    declared_bytes: int | None = int(content_length_header)
                except ValueError:
                    declared_bytes = None
                if declared_bytes is not None and declared_bytes > _MAX_RESPONSE_BYTES:
                    raise FetchResponseTooLargeError(
                        f"response too large (content-length="
                        f"{content_length_header}): {url_str}",
                        actual_bytes=declared_bytes,
                        limit_bytes=_MAX_RESPONSE_BYTES,
                    )
            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise FetchResponseTooLargeError(
                    f"response too large ({len(response.content)} bytes): {url_str}",
                    actual_bytes=len(response.content),
                    limit_bytes=_MAX_RESPONSE_BYTES,
                )

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                logger.info("content_not_html", url=url_str, content_type=content_type)
                return ExtractionEmpty(reason="not_html")

            try:
                html_text = _decode_html_response(response)
                return await asyncio.to_thread(_extract_from_html, html_text, url_str)
            except Exception as e:
                logger.warning("content_parse_error", url=url_str, error=str(e))
                return ExtractionEmpty(reason="parse_error")
