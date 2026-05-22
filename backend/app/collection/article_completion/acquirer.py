"""HTML 取得層 (acquisition) — URL から記事本文と公開日時を取得する。

呼び出し側は ``URL -> HtmlAcquisitionResult`` の契約にのみ依存する。恒久的な
失敗と一時的な失敗は例外として分離し、呼び出し側で扱いを切り分けられる。
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

from app.collection.article_completion.acquisition_failure import (
    AcquisitionCrashed,
    AcquisitionFailure,
    NotHtml,
    ParserRejected,
    QualityGateFailed,
)
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MIN_LENGTH as _BODY_MIN_LENGTH,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.external_fetch_errors import (
    FetchRedirectBlockedError,
    FetchResponseTooLargeError,
    FetchRobotsDisallowedError,
)
from app.collection.source_fetch.tools.http_error_translation import (
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


@dataclass(frozen=True, slots=True)
class RawResponse:
    """取得段 (_fetch) が返す httpx 非依存の中間値。

    ``httpx.Response`` を ``_fetch`` 内に封じ込め、抽出段 (_extract) を httpx に
    依存させず単体テスト可能にするための値。``decoded_text`` は httpx の
    ``TextDecoder`` (Content-Type charset があればそれ、なければ UTF-8) で
    デコード済みの本文で、抽出側で同じ文字列を再現するために持ち越す。
    """

    url: str
    content_type: str
    charset_from_header: str | None
    content: bytes
    decoded_text: str


def _decode_html_response(raw: RawResponse) -> str:
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
    if raw.charset_from_header is not None:
        return raw.decoded_text

    # HTML 先頭バイトから meta charset を探す
    content_bytes = raw.content
    head = content_bytes[:_SNIFF_BYTES]

    match = _META_CHARSET_RE.search(head) or _META_HTTP_EQUIV_CHARSET_RE.search(head)
    if match:
        encoding = match.group(1).decode("ascii", errors="ignore").strip()
        try:
            return content_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            logger.warning(
                "html_charset_decode_failed",
                declared_charset=encoding,
                url=raw.url,
            )

    # meta charset もなければ httpx のデフォルト（UTF-8）にフォールバック
    return raw.decoded_text


@dataclass(frozen=True)
class AcquiredContent:
    """取得成功: 品質ゲートを通過した本文・タイトル。

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


HtmlAcquisitionResult = AcquiredContent | AcquisitionFailure


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

        ``client`` は ``make_safe_async_client`` 経由なので本メソッド内の
        ``client.get`` でも SSRF 検証が走る。
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


def _parse_html(html: str, url: str) -> HtmlAcquisitionResult:
    """trafilatura で HTML をパースし本文・公開日時を取り出す（同期・CPU バウンド）。

    抽出段 ``_extract`` の parse ステップ。失敗は ``ParserRejected`` /
    ``QualityGateFailed`` で表す (例外は呼び出し元 ``_extract`` が ``stage="parse"``
    の crash に畳む)。``_extract`` ごと ``asyncio.to_thread()`` でオフロードされる。
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
        logger.info("parser_rejected", url=url)
        return ParserRejected()

    # trafilatura 2.0 以降、bare_extraction() は Document インスタンスを返す
    # 本文の品質ゲート: 50 文字未満は棄却
    text = result.text
    body_stripped = text.strip() if text else ""
    body = body_stripped if len(body_stripped) >= _BODY_MIN_LENGTH else None

    # タイトル: trafilatura が OGP / Twitter Card / JSON-LD / <title> / h1 の順で抽出。
    # HTML タグ除去と 500 文字上限で整形し、空なら None。
    cleaned_title = strip_html_tags(result.title)
    title = cleaned_title[:_TITLE_MAX_LENGTH] if cleaned_title else None

    if body is None or title is None:
        title_present = title is not None
        body_length = len(body_stripped)
        # paywall stub / 拒否ページ判別に使えるよう、本文がゼロでも閾値未満でも
        # ない (= title 欠落で落ちた) 場合は冒頭断片を残さない。
        body_sample = body_stripped if 0 < body_length < _BODY_MIN_LENGTH else None
        logger.info(
            "quality_gate_failed",
            url=url,
            body_length=body_length,
            title_present=title_present,
        )
        return QualityGateFailed(
            body_length=body_length,
            title_present=title_present,
            body_sample=body_sample,
        )

    return AcquiredContent(
        title=title,
        body=body,
        published_at=PublishedAt.parse(result.date),
    )


class ArticleHtmlAcquirer:
    """URL から記事本文と公開日時を取得する取得器。

    呼び出し側は ``acquire(url) -> HtmlAcquisitionResult`` の契約のみに依存する。
    内部は取得 (_fetch: 接続できたか?) と抽出 (_extract: HTML として読めたか?)
    の二段に分かれ、両者の境界に httpx 非依存の ``RawResponse`` を挟む。
    robots キャッシュや HTTP クライアントのライフサイクルは内部で完結する。
    """

    def __init__(self) -> None:
        self._robots_cache = _RobotsCache()

    async def acquire(self, url: SafeUrl) -> HtmlAcquisitionResult:
        """指定 URL の HTML から記事本文・タイトル・公開日時を取得する。

        取得 (_fetch) で接続成否を確定し、成功した RawResponse を抽出 (_extract)
        に渡す。抽出は CPU バウンドなので ``asyncio.to_thread`` でオフロードする。

        Returns:
            HtmlAcquisitionResult: ``AcquiredContent``（成功）または
            ``AcquisitionFailure``（Content-Type 不一致 / パーサ拒否 / decode|parse
            例外 / 品質ゲート未達。証拠は variant に畳む）。

        Raises:
            ExternalFetchError: robots Disallow / redirect block / response 過大 /
            HTTP status (4xx/5xx) / transport (timeout/network) / SSRF block。
            どの origin failure かは subclass で表現する (本層は retry/terminal を
            分類しない)。
        """
        raw = await self._fetch(url)
        return await asyncio.to_thread(self._extract, raw)

    async def _fetch(self, url: SafeUrl) -> RawResponse:
        """取得段: 接続できたか? に純化する。

        成功すれば ``RawResponse`` を返し、失敗は ``ExternalFetchError`` を raise
        する非対称な二値。``httpx.Response`` は本メソッド内に封じ込め、redirect /
        status / size 判定もここで消費する。

        Raises:
            ExternalFetchError: robots Disallow / redirect block / response 過大 /
            HTTP status (4xx/5xx) / transport (timeout/network) / SSRF block。
        """
        url_str = str(url)

        # SSRF defense は make_safe_async_client の event_hook に集約済み
        # (全 client.get 直前で host が public か検証)。HTTP status /
        # transport / SSRF の origin error 翻訳は translate_fetch_exception
        # に一本化し、本層で status_code を直書き分類しない。
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

            # httpx.Response をここで消費し RawResponse に畳む。decoded_text は
            # response.text を一度だけ評価して持ち越す (抽出側で httpx の charset
            # 挙動を再実装しないため)。
            return RawResponse(
                url=str(response.url),
                content_type=response.headers.get("content-type", ""),
                charset_from_header=response.charset_encoding,
                content=response.content,
                decoded_text=response.text,
            )

    def _extract(self, raw: RawResponse) -> HtmlAcquisitionResult:
        """抽出段: HTML として読み本文化できたか?。

        同期・例外を投げない層。Content-Type 判定 / decode / trafilatura の各失敗を
        ``AcquisitionFailure`` variant に畳む。``acquire`` から to_thread で丸ごと
        オフロードされるため decode/parse の例外もこの thread 内で捕捉できる。
        """
        if "text/html" not in raw.content_type:
            logger.info("content_not_html", url=raw.url, content_type=raw.content_type)
            return NotHtml(content_type=raw.content_type)

        try:
            html_text = _decode_html_response(raw)
        except Exception as e:
            logger.warning(
                "content_parse_error",
                url=raw.url,
                stage="decode",
                error=str(e),
            )
            return AcquisitionCrashed(
                stage="decode",
                error_class=type(e).__name__,
                error_message=str(e),
            )

        try:
            return _parse_html(html_text, raw.url)
        except Exception as e:
            logger.warning(
                "content_parse_error",
                url=raw.url,
                stage="parse",
                error=str(e),
            )
            return AcquisitionCrashed(
                stage="parse",
                error_class=type(e).__name__,
                error_message=str(e),
            )
