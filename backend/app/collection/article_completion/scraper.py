"""URL から記事本文と公開日時を取得する scrape concern。"""

from __future__ import annotations

import asyncio
import html
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

from app.collection.article_acquisition.tools.http_error_translation import (
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
from app.shared.security.safe_url import SafeUrl
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
# 取得時点のレスポンスサイズ上限。抽出後の本文長とは別関心。
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
USER_AGENT = "VectorBot/1.0 (+https://github.com/vector-news)"
HEADERS = {"User-Agent": USER_AGENT}

# HTML meta charset を先頭バイト列から検出する。
_META_CHARSET_RE = re.compile(
    rb'<meta\s+charset\s*=\s*["\']?\s*([^"\'\s;>]+)', re.IGNORECASE
)
_META_HTTP_EQUIV_CHARSET_RE = re.compile(rb"charset\s*=\s*([^\s\"';>]+)", re.IGNORECASE)
_SNIFF_BYTES = 2048

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(text: str | None) -> str | None:
    """scrape した title 文字列から HTML タグを除去し entity を decode する。"""
    if text is None:
        return None
    cleaned = _HTML_TAG_RE.sub("", text)
    return html.unescape(cleaned).strip()


@dataclass(frozen=True, slots=True)
class RawResponse:
    """``_fetch`` が返す httpx 非依存の中間値。"""

    url: str
    content_type: str
    charset_from_header: str | None
    content: bytes
    decoded_text: str


def _decode_html_response(raw: RawResponse) -> str:
    """HTTP charset が無い場合だけ HTML meta charset を sniff して decode する。"""
    if raw.charset_from_header is not None:
        return raw.decoded_text

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

    return raw.decoded_text


@dataclass(frozen=True)
class ScrapedContent:
    """取得成功: 品質ゲートを通過した本文・タイトル。"""

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
        """素材が品質ゲートを満たせば ``ScrapedContent``、無理なら失敗値を返す。"""
        cleaned_title = _strip_html_tags(raw_title)
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
    """robots.txt に基づき取得可否を判定する関所。"""

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def is_fetch_allowed(
        self,
        client: httpx.AsyncClient,  # noqa: TID251
        url: str,
    ) -> bool:
        """URL を取得してよいか robots.txt に照らして判定する。"""
        parsed = urlparse(url)
        domain = parsed.netloc
        robots_url = f"{parsed.scheme}://{domain}/robots.txt"

        async with self._locks[domain]:
            if domain in self._cache:
                rp = self._cache[domain]
                return rp is None or rp.can_fetch(USER_AGENT, url)

            try:
                resp = await client.get(robots_url, timeout=10.0)
                if resp.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(resp.text.splitlines())
                    self._cache[domain] = rp
                else:
                    self._cache[domain] = None
            except httpx.HTTPError:
                self._cache[domain] = None

            rp = self._cache[domain]
            return rp is None or rp.can_fetch(USER_AGENT, url)


def _parse_raw_response_as_html_document(
    raw: RawResponse,
) -> TrafilaturaDocument | ParseFailure:
    """RawResponse を HTML document として解釈し、失敗は値で返す。"""
    if "text/html" not in raw.content_type:
        logger.info("content_not_html", url=raw.url, content_type=raw.content_type)
        return NotHtml(content_type=raw.content_type)

    # decode 失敗は fallback に畳み、ParseCrashed は trafilatura parse 専用に保つ。
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
        # as_dict=False では Document が期待値。想定外型は ParseCrashed に畳む。
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
    """``TrafilaturaDocument`` を primitives に射影し品質ゲートに通す。"""
    text = document.text
    body_stripped = text.strip() if text else ""

    # 文字化け疑いは結果を変えず metric だけ残す。
    replacement_char_count = body_stripped.count("�")
    if replacement_char_count > 0:
        logger.warning(
            "mojibake_detected",
            url=url,
            replacement_char_count=replacement_char_count,
            replacement_char_ratio=replacement_char_count / len(body_stripped),
            body_length=len(body_stripped),
        )

    # 品質ゲート判定は ScrapedContent.try_create に集約する。
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
    """URL から記事本文と公開日時を取得する scraper。"""

    def __init__(self) -> None:
        self._robots_gate = _RobotsGate()

    async def scrape(self, url: SafeUrl) -> ScrapedContent | ScrapeFailure:
        """HTML 取得・抽出を行い、成功値または scrape failure value を返す。"""
        try:
            raw = await self._fetch(url)
        except ExternalFetchError as exc:
            return FetchFailed(error=exc)
        return await asyncio.to_thread(self._extract_content_from_response, raw)

    async def _fetch(self, url: SafeUrl) -> RawResponse:
        """HTTP 取得を行い、失敗は ``ExternalFetchError`` として raise する。"""
        url_str = str(url)

        # SSRF defense は make_safe_async_client の event_hook で行う。
        async with make_safe_async_client(
            headers=HEADERS, timeout=HTTP_TIMEOUT
        ) as client:
            try:
                if await self._robots_gate.is_fetch_allowed(client, url_str):
                    response = await client.get(url_str, timeout=HTTP_TIMEOUT)
                    # 3xx は raise_for_status で拾われないため明示的に弾く。
                    # follow_redirects=False が Location 経由 SSRF を遮断する。
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

            # Content-Length 自己申告 + 実バイト数の二段でサイズ上限を守る。
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

            # httpx.Response を RawResponse に畳み、抽出側を httpx から切り離す。
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
        """RawResponse から ScrapedContent または content failure value を得る。"""
        parsed = _parse_raw_response_as_html_document(raw)
        if not isinstance(parsed, TrafilaturaDocument):
            return parsed  # ParseFailure (NotHtml | ParserGaveUp | ParseCrashed)
        return _build_scraped_content_from_document(parsed, url=raw.url)
