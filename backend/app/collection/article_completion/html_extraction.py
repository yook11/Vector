"""取得済みHTMLから素材を抽出し、新経路用の失敗を伝える。"""

import re
from datetime import UTC, datetime

import trafilatura
from trafilatura.settings import Document as TrafilaturaDocument

from app.collection.article_completion.content import RawResponse, ScrapedContent
from app.collection.article_completion.errors import (
    ArticleContentTypeError,
    ArticleExtractionCrashedError,
    ArticleExtractionCrashReason,
    ArticleExtractionEmptyError,
)

# HTML meta charset を先頭バイト列から検出する。
_META_CHARSET_RE = re.compile(
    rb'<meta\s+charset\s*=\s*["\']?\s*([^"\'\s;>]+)', re.IGNORECASE
)
_META_HTTP_EQUIV_CHARSET_RE = re.compile(rb"charset\s*=\s*([^\s\"';>]+)", re.IGNORECASE)
_SNIFF_BYTES = 2048


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
            pass

    return raw.decoded_text


def extract_html_content(raw: RawResponse) -> ScrapedContent:
    """HTMLを抽出素材に変換し、抽出器の失敗だけを境界で分類する。"""
    media_type = (raw.content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "text/html":
        raise ArticleContentTypeError(content_type=raw.content_type)

    html = _decode_html_response(raw)
    date_extraction_params = {
        "original_date": True,
        "extensive_search": True,
        "max_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "outputformat": "%Y-%m-%dT%H:%M:%S",
    }
    try:
        result = trafilatura.bare_extraction(
            html,
            url=raw.url,
            favor_precision=True,
            include_comments=False,
            include_tables=True,
            deduplicate=False,
            with_metadata=True,
            date_extraction_params=date_extraction_params,
        )
    except Exception as exc:
        raise ArticleExtractionCrashedError(
            reason=ArticleExtractionCrashReason.EXCEPTION
        ) from exc

    if result is None:
        raise ArticleExtractionEmptyError()
    if not isinstance(result, TrafilaturaDocument):
        raise ArticleExtractionCrashedError(
            reason=ArticleExtractionCrashReason.UNEXPECTED_RESULT
        )

    return ScrapedContent.from_extraction(
        raw_title=result.title,
        stripped_body=result.text if result.text is not None else "",
        raw_date=result.date,
    )
