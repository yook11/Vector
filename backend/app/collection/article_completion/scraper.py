"""HTML scrape 層 — URL から記事本文と公開日時を取得する。

公開境界 ``scrape`` は never raise の二値 (``ScrapedContent | ScrapeFailure``)。
transport 失敗は内部 ``_fetch`` が ``ExternalFetchError`` を raise し、``scrape`` が
境界でそれを ``FetchFailed`` 値に畳む。content 失敗 (Content-Type 不一致 / パーサ拒否 /
parse 例外 / 品質ゲート未達) は content extraction 段
(parse: ``_parse_raw_response_as_html_document`` /
build: ``_build_scraped_content_from_document``) が ``ContentFailure`` 値で返す。
呼び出し側は分類済みの値だけを受け取る。
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
from trafilatura.settings import Document as TrafilaturaDocument

from app.collection.article_collection.tools.http_error_translation import (
    translate_fetch_exception,
)
from app.collection.article_completion.scrape_failure import (
    ContentFailure,
    ContentQualityTooLow,
    FetchFailed,
    NotHtml,
    ParseCrashed,
    ParseFailure,
    ParserGaveUp,
    ScrapeFailure,
)
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MIN_LENGTH as _BODY_MIN_LENGTH,
)
from app.collection.domain.article_limits import (
    ARTICLE_TITLE_MAX_LENGTH as _TITLE_MAX_LENGTH,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchRedirectBlockedError,
    FetchResponseTooLargeError,
    FetchRobotsDisallowedError,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
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

    ``httpx.Response`` を ``_fetch`` 内に封じ込め、content extraction 段
    (_extract_content_from_response) を httpx に依存させず単体テスト可能にするための値。
    ``decoded_text`` は httpx の
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
class ScrapedContent:
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

    @classmethod
    def try_create(
        cls,
        *,
        raw_title: str | None,
        stripped_body: str,
        raw_date: str | None,
    ) -> ScrapedContent | ContentQualityTooLow:
        """素材から品質ゲートを満たすときのみ ScrapedContent を構築する。

        ゲート判定 (本文 50 文字以上 + 非空タイトル ≤500) の SSoT。満たさなければ
        証拠付き ``ContentQualityTooLow`` を値で返す。strict コンストラクタの invariant
        (``__post_init__``) は本 factory が必ず満たす backstop。副作用なし (ログは
        呼び出し側 ``_build_scraped_content_from_document``)。``stripped_body`` は
        strip 済み本文を受ける。
        """
        cleaned_title = strip_html_tags(raw_title)
        title = cleaned_title[:_TITLE_MAX_LENGTH] if cleaned_title else None
        body = stripped_body if len(stripped_body) >= _BODY_MIN_LENGTH else None

        if body is None or title is None:
            body_length = len(stripped_body)
            # paywall stub / 拒否ページ判別に使えるよう、本文がゼロでも閾値以上でも
            # ない (= title 欠落で落ちた) 場合は冒頭断片を残さない。
            body_sample = stripped_body if 0 < body_length < _BODY_MIN_LENGTH else None
            return ContentQualityTooLow(
                body_length=body_length,
                title_present=title is not None,
                body_sample=body_sample,
            )

        return cls(title=title, body=body, published_at=PublishedAt.parse(raw_date))


class _RobotsGate:
    """robots.txt に基づき取得可否を判定する関所 (並行アクセス対応)。

    判定材料の robots.txt はドメイン単位でキャッシュし、同じドメインを複数
    コルーチンが同時参照した際の重複フェッチを ``asyncio.Lock`` で防ぐ。
    """

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def is_fetch_allowed(
        self,
        client: httpx.AsyncClient,  # noqa: TID251
        url: str,
    ) -> bool:
        """URL を取得してよいか robots.txt に照らして判定する。許可なら True。

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


def _parse_raw_response_as_html_document(
    raw: RawResponse,
) -> TrafilaturaDocument | ParseFailure:
    """RawResponse を HTML document として解釈できるか? に純化する（同期・CPU）。

    content-type 判定と decode を済ませ、trafilatura で parse して
    ``TrafilaturaDocument`` を得る。失敗はすべて値で返す: Content-Type 不一致は
    ``NotHtml``、パーサ拒否 (``None``) は ``ParserGaveUp``、parse 例外と想定外戻り型は
    ``ParseCrashed``。``_extract_content_from_response`` ごと ``asyncio.to_thread()``
    でオフロードされる。
    """
    if "text/html" not in raw.content_type:
        logger.info("content_not_html", url=raw.url, content_type=raw.content_type)
        return NotHtml(content_type=raw.content_type)

    # decode は input 起因の例外を出さない (charset 不一致は内部で握り UTF-8 fallback)
    # ため try の外。ParseCrashed は trafilatura parse 専用に保つ。
    html = _decode_html_response(raw)

    try:
        result = trafilatura.bare_extraction(
            html,
            url=raw.url,
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
            logger.info("parser_gave_up", url=raw.url)
            return ParserGaveUp()
        # bare_extraction は as_dict=False (本呼び出しの default) のとき Document
        # を返す。signature 上の dict 分岐は as_dict=True 専用。想定外の型は parse
        # 故障として raise せず ParseCrashed を値で返す (caller 依存の失敗契約なし)。
        if not isinstance(result, TrafilaturaDocument):
            message = (
                f"bare_extraction returned unexpected type: {type(result).__name__}"
            )
            logger.warning("content_parse_error", url=raw.url, error=message)
            return ParseCrashed(error_class="TypeError", error_message=message)
    except Exception as e:
        logger.warning("content_parse_error", url=raw.url, error=str(e))
        return ParseCrashed(error_class=type(e).__name__, error_message=str(e))

    return result


def _build_scraped_content_from_document(
    document: TrafilaturaDocument,
    *,
    url: str,
) -> ScrapedContent | ContentQualityTooLow:
    """``TrafilaturaDocument`` を品質ゲートに通して ``ScrapedContent`` を組む。

    ``TrafilaturaDocument`` (trafilatura foreign type) に触れる唯一の場所。受け取った
    document は即座に primitives (``title`` / ``text`` / ``date``) へ射影してから
    ``ScrapedContent.try_create`` に渡し、``TrafilaturaDocument`` を try_create や
    公開境界へ漏らさない (parse↔build 境界に閉じ込める)。
    """
    text = document.text
    body_stripped = text.strip() if text else ""

    # 文字化け観測 (Phase 1 = ログのみ): charset 不明で UTF-8 fallback した本文に
    # 置換文字 (U+FFFD) が残ると、品質ゲートを通過して silent に成功扱いになりうる。
    # 結果は変えず生 metric だけ残し、成功・品質失敗どちらのパスでも発火させる。
    replacement_char_count = body_stripped.count("�")
    if replacement_char_count > 0:
        logger.warning(
            "mojibake_detected",
            url=url,
            replacement_char_count=replacement_char_count,
            replacement_char_ratio=replacement_char_count / len(body_stripped),
            body_length=len(body_stripped),
        )

    # 品質ゲート判定は ScrapedContent.try_create が SSoT。本関数は素材を渡し、
    # 失敗値の log emit だけを担う (factory は純粋)。
    outcome = ScrapedContent.try_create(
        raw_title=document.title,
        stripped_body=body_stripped,
        raw_date=document.date,
    )
    if isinstance(outcome, ContentQualityTooLow):
        logger.info(
            "content_quality_too_low",
            url=url,
            body_length=outcome.body_length,
            title_present=outcome.title_present,
        )
    return outcome


class ArticleScraper:
    """URL から記事本文と公開日時を取得する scraper。

    呼び出し側は ``scrape(url) -> ScrapedContent | ScrapeFailure`` の契約のみに
    依存する。内部は取得 (_fetch) と content extraction
    (_extract_content_from_response) の二段で、境界に httpx 非依存の ``RawResponse`` を
    挟む。robots キャッシュと HTTP クライアントのライフサイクルは内部で完結する。
    """

    def __init__(self) -> None:
        self._robots_gate = _RobotsGate()

    async def scrape(self, url: SafeUrl) -> ScrapedContent | ScrapeFailure:
        """指定 URL の HTML から記事本文・タイトル・公開日時を取得する (never raise)。

        抽出は CPU バウンドなので ``asyncio.to_thread`` でオフロードする。公開境界
        として transport 失敗を値化する: 内部 ``_fetch`` が raise する
        ``ExternalFetchError`` (robots Disallow / redirect block / response 過大 /
        HTTP status / transport / SSRF block) を捕え ``FetchFailed`` に畳む。

        Returns:
            ``ScrapedContent`` (成功) または ``ScrapeFailure`` —
            ``FetchFailed`` (transport) / ``NotHtml`` (Content-Type 不一致) /
            ``ParserGaveUp`` (パーサ拒否) / ``ParseCrashed`` (parse 例外) /
            ``ContentQualityTooLow`` (品質ゲート未達)。本層は retry/terminal を
            分類しない (証拠だけを値で返す)。
        """
        try:
            raw = await self._fetch(url)
        except ExternalFetchError as exc:
            return FetchFailed(error=exc)
        return await asyncio.to_thread(self._extract_content_from_response, raw)

    async def _fetch(self, url: SafeUrl) -> RawResponse:
        """取得段: 接続できたか? に純化する。

        成功すれば ``RawResponse`` を返し、失敗は ``ExternalFetchError`` を raise
        する非対称な二値。``httpx.Response`` は本メソッド内に封じ込め、redirect /
        status / size 判定もここで消費する。
        """
        url_str = str(url)

        # SSRF defense は make_safe_async_client の event_hook に集約 (client.get
        # 直前で host を検証)。origin error 翻訳は translate_fetch_exception に
        # 一本化し、本層で status_code を直書き分類しない。
        async with make_safe_async_client(
            headers=HEADERS, timeout=HTTP_TIMEOUT
        ) as client:
            try:
                if await self._robots_gate.is_fetch_allowed(client, url_str):
                    response = await client.get(url_str, timeout=HTTP_TIMEOUT)
                    # 3xx は raise_for_status で拾われないので明示的に弾く。
                    # follow_redirects=False (client default) が
                    # Location 経由 SSRF を遮断。
                    if 300 <= response.status_code < 400:
                        logger.info(
                            "redirect_not_followed",
                            url=url_str,
                            status=response.status_code,
                            location=response.headers.get("location", "")[:200],
                        )
                        raise FetchRedirectBlockedError(
                            f"redirect not followed: HTTP "
                            f"{response.status_code}: {url_str}"
                        )
                    response.raise_for_status()
                else:
                    raise FetchRobotsDisallowedError(f"robots.txt blocked: {url_str}")
            except (httpx.HTTPError, HostBlockedError, HostResolutionError) as e:
                raise translate_fetch_exception(e, source_name=url_str) from e

            # レスポンスサイズ上限: 巨大レスポンスを引き出す攻撃面を閉じる
            # (Content-Length 自己申告 + 実バイト数の二段)。
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

            # httpx.Response を消費し RawResponse に畳む。decoded_text は
            # response.text を一度評価して持ち越す (抽出側で charset 挙動を再現しない)。
            return RawResponse(
                url=str(response.url),
                content_type=response.headers.get("content-type", ""),
                charset_from_header=response.charset_encoding,
                content=response.content,
                decoded_text=response.text,
            )

    def _extract_content_from_response(
        self, raw: RawResponse
    ) -> ScrapedContent | ContentFailure:
        """content extraction: RawResponse から ScrapedContent を得る (同期)。

        parse (RawResponse → TrafilaturaDocument) と build (TrafilaturaDocument →
        ScrapedContent) を順に呼ぶユースケース。失敗はすべて値で返る。本段は
        ネットワークを持たないため transport 失敗 (``FetchFailed``) は構造的に返せず、
        戻り型は content 失敗 (``ContentFailure``) のみで固定される。
        """
        parsed = _parse_raw_response_as_html_document(raw)
        if not isinstance(parsed, TrafilaturaDocument):
            return parsed  # ParseFailure (NotHtml | ParserGaveUp | ParseCrashed)
        return _build_scraped_content_from_document(parsed, url=raw.url)
