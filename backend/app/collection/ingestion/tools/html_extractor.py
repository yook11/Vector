"""HTML 取得・抽出の道具 — 旧 ``ArticleHtmlExtractor`` のロジックをコピー新設。

collection-acquisition-redesign Phase 0c。旧 extractor は
``ExtractedContent | ExtractionEmpty`` の sum 型を返していたが、本道具は
**例外で失敗を表現** する: 上流 Fetcher が ``FetchOutcome.Failed`` を構築する
際の分類軸 (``FailureCode``) と直接対応させるため、抽出失敗 / 品質ゲート未達
の概念を Failed 側に統一する。

旧 ``ArticleHtmlExtractor`` (``app/collection/extraction/extractor.py``) は
Phase 2a まで温存し物理削除しない。本モジュールは意図的なロジックコピーで
あり、運用窓中の二重管理コストは Phase 2a 完了まで受け入れる
(`spec collection-acquisition-redesign-plan.md §PR-0c`).
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

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain.value_objects import PublishedAt
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
_TITLE_MAX_LENGTH = 500
_BODY_MIN_LENGTH = 50
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB
USER_AGENT = "VectorBot/1.0 (+https://github.com/vector-news)"
HEADERS = {"User-Agent": USER_AGENT}

_META_CHARSET_RE = re.compile(
    rb'<meta\s+charset\s*=\s*["\']?\s*([^"\'\s;>]+)', re.IGNORECASE
)
_META_HTTP_EQUIV_CHARSET_RE = re.compile(rb"charset\s*=\s*([^\s\"';>]+)", re.IGNORECASE)
_SNIFF_BYTES = 2048


ExtractionFailureKind = Literal["not_html", "parse_error", "quality_gate"]


class ExtractionEmptyError(Exception):
    """trafilatura が抽出不能 / Content-Type 不一致 / 品質ゲート未達。

    旧 ``ExtractionEmpty`` (sum 型の Empty 側) を例外化したもの。Fetcher 側で
    ``FailureCode.extraction_empty`` (parse_error / not_html) または
    ``body_too_short`` / ``title_missing`` (quality_gate) に分類する。
    """

    def __init__(self, kind: ExtractionFailureKind) -> None:
        super().__init__(kind)
        self.kind: ExtractionFailureKind = kind


@dataclass(frozen=True)
class ExtractedContent:
    """抽出成功: 品質ゲートを通過した本文・タイトル。

    旧 extractor の ``ExtractedContent`` と invariant は同一 (title 非空 +
    500 文字以内 / body 50 文字以上 / published_at は任意で None 許容)。
    Fetcher 側で ``published_at is None`` を ``FailureCode.published_at_missing``
    に分岐させるため、ここでは構造的必須化はしない。
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


def _decode_html_response(response: httpx.Response) -> str:
    """HTTP レスポンスの HTML を正しいエンコーディングでデコードする。

    httpx は HTTP Content-Type ヘッダーの charset を優先し、無ければ UTF-8
    にフォールバックする。日本語サイト (IT media 等) は HTTP ヘッダーに
    charset がなく HTML の ``<meta charset="Shift_JIS">`` でのみ宣言する
    ケースがあり、その場合 UTF-8 デフォルトで文字化けする。本関数は
    Content-Type に charset がない場合のみ HTML 先頭バイトから meta charset
    をスニッフィングする。
    """
    if response.charset_encoding is not None:
        return response.text

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

    return response.text


class _RobotsCache:
    """並行アクセス対応の robots.txt キャッシュ。

    同じドメインを複数コルーチンが同時参照した際の重複フェッチを防ぐため、
    ドメイン単位の ``asyncio.Lock`` を用いる。
    """

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check(self, client: httpx.AsyncClient, url: str) -> bool:  # noqa: TID251
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


def _extract_from_html(html: str, url: str) -> ExtractedContent:
    """trafilatura で HTML から本文と公開日時を抽出する (同期, CPU バウンド)。

    本関数は ``asyncio.to_thread`` 経由で呼ぶ前提。失敗時は
    ``ExtractionEmptyError`` を raise する。
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
        raise ExtractionEmptyError("parse_error")

    text = result.text
    body = text.strip() if text and len(text.strip()) >= _BODY_MIN_LENGTH else None

    cleaned_title = strip_html_tags(result.title)
    title = cleaned_title[:_TITLE_MAX_LENGTH] if cleaned_title else None

    if body is None or title is None:
        raise ExtractionEmptyError("quality_gate")

    return ExtractedContent(
        title=title,
        body=body,
        published_at=PublishedAt.parse(result.date),
    )


class HtmlContentExtractor:
    """URL から記事本文・タイトル・公開日時を取得する道具。

    呼び出し側 (Fetcher) は ``fetch_and_extract(url) -> ExtractedContent`` の
    契約のみに依存する。失敗は例外で表現:

    - ``PermanentFetchError``: robots.txt 拒否 / 403 / 404 / 410 / 451 / 過大レスポンス
    - ``TemporaryFetchError``: 5xx / 429 / タイムアウト / ネットワーク / DNS 一時失敗
    - ``ExtractionEmptyError``: Content-Type 不一致 / parse 失敗 / 品質ゲート未達

    robots キャッシュと httpx クライアントのライフサイクルは内部で完結する。
    """

    def __init__(self) -> None:
        self._robots_cache = _RobotsCache()

    async def fetch_and_extract(self, url: SafeUrl) -> ExtractedContent:
        url_str = str(url)

        async with make_safe_async_client(
            headers=HEADERS, timeout=HTTP_TIMEOUT
        ) as client:
            try:
                if not await self._robots_cache.check(client, url_str):
                    raise PermanentFetchError(f"robots.txt blocked: {url_str}")

                try:
                    response = await client.get(url_str, timeout=HTTP_TIMEOUT)
                    if 300 <= response.status_code < 400:
                        logger.info(
                            "redirect_not_followed",
                            url=url_str,
                            status=response.status_code,
                            location=response.headers.get("location", "")[:200],
                        )
                        raise PermanentFetchError(
                            f"redirect not followed: HTTP "
                            f"{response.status_code}: {url_str}"
                        )
                    response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code
                    if status in (403, 404, 410, 451):
                        raise PermanentFetchError(f"HTTP {status}: {url_str}") from e
                    raise TemporaryFetchError(f"HTTP {status}: {url_str}") from e
                except httpx.RequestError as e:
                    raise TemporaryFetchError(f"request error: {url_str}: {e}") from e

                content_length_header = response.headers.get("content-length")
                if content_length_header is not None:
                    try:
                        if int(content_length_header) > _MAX_RESPONSE_BYTES:
                            raise PermanentFetchError(
                                f"response too large (content-length="
                                f"{content_length_header}): {url_str}"
                            )
                    except ValueError:
                        pass
                if len(response.content) > _MAX_RESPONSE_BYTES:
                    raise PermanentFetchError(
                        f"response too large ({len(response.content)} bytes): {url_str}"
                    )

                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type:
                    logger.info(
                        "content_not_html", url=url_str, content_type=content_type
                    )
                    raise ExtractionEmptyError("not_html")

                try:
                    html_text = _decode_html_response(response)
                    return await asyncio.to_thread(
                        _extract_from_html, html_text, url_str
                    )
                except ExtractionEmptyError:
                    raise
                except Exception as e:
                    logger.warning("content_parse_error", url=url_str, error=str(e))
                    raise ExtractionEmptyError("parse_error") from e
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e
