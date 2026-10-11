"""取得済みHTMLから記事の項目を抽出し、補完器へ渡す抽出結果か失敗の理由を伝える。"""

import html
import re
from datetime import UTC, datetime
from enum import StrEnum

import trafilatura
from trafilatura.settings import Document as TrafilaturaDocument

from app.collection.article_completion.article_completer import ExtractedContent
from app.collection.article_completion.content import RawResponse
from app.collection.domain.article_limits import ARTICLE_TITLE_MAX_LENGTH
from app.collection.domain.value_objects import PublishedAt
from app.shared.errors import ApplicationError, ApplicationErrorValue


class HtmlExtractionFailureReason(StrEnum):
    CONTENT_TYPE_UNSUPPORTED = "content_type_unsupported"
    RESULT_EMPTY = "extraction_empty"
    EXTRACTOR_RAISED = "extraction_failed"
    RESULT_TYPE_MISMATCH = "extraction_result_unexpected"
    HTML_DECODE_FAILED = "html_decode_failed"
    MATERIAL_CONVERSION_FAILED = "extraction_material_conversion_failed"


class HtmlExtractionError(ApplicationError):
    def __init__(
        self,
        *,
        reason: HtmlExtractionFailureReason,
        message: str,
        details: dict[str, ApplicationErrorValue] | None = None,
    ) -> None:
        super().__init__(message, details={**(details or {}), "reason": reason.value})
        self.reason = reason


# HTML meta charset を先頭バイト列から検出する。
_META_CHARSET_RE = re.compile(
    rb'<meta\s+charset\s*=\s*["\']?\s*([^"\'\s;>]+)', re.IGNORECASE
)
_META_HTTP_EQUIV_CHARSET_RE = re.compile(rb"charset\s*=\s*([^\s\"';>]+)", re.IGNORECASE)
_SNIFF_BYTES = 2048
_HTML_TAG_RE = re.compile(r"<[^>]+>")


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


def _strip_html_tags(text: str | None) -> str | None:
    """scrape した title 文字列から HTML タグを除去し entity を decode する。"""
    if text is None:
        return None
    cleaned = _HTML_TAG_RE.sub("", text)
    return html.unescape(cleaned).strip()


def _to_extracted_content(document: TrafilaturaDocument) -> ExtractedContent:
    """抽出値を整形して空の項目をNoneとし、完成記事の品質判定は補完器に委ねる。"""
    cleaned_title = _strip_html_tags(document.title)
    body = (document.text or "").strip()
    return ExtractedContent(
        title=cleaned_title[:ARTICLE_TITLE_MAX_LENGTH] if cleaned_title else None,
        body=body or None,
        published_at=PublishedAt.parse(document.date),
    )


def extract_html_content(raw: RawResponse) -> ExtractedContent:
    """HTMLを抽出結果に変換し、失敗した処理の理由と元の原因を伝える。"""
    media_type = (raw.content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "text/html":
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.CONTENT_TYPE_UNSUPPORTED,
            message="応答をHTMLとして扱えないため、記事を抽出できません",
            details={"content_type": raw.content_type},
        )

    try:
        html = _decode_html_response(raw)
    except HtmlExtractionError:
        raise
    except Exception as exc:
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.HTML_DECODE_FAILED,
            message="HTMLを文字列へ変換する処理で例外が発生しました",
        ) from exc
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
    except HtmlExtractionError:
        raise
    except Exception as exc:
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.EXTRACTOR_RAISED,
            message="記事の抽出器を実行中に例外が発生しました",
        ) from exc

    if result is None:
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.RESULT_EMPTY,
            message="抽出器から記事の素材が得られませんでした",
        )
    if not isinstance(result, TrafilaturaDocument):
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.RESULT_TYPE_MISMATCH,
            message="抽出器がDocument型ではない結果を返しました",
        )

    try:
        return _to_extracted_content(result)
    except HtmlExtractionError:
        raise
    except Exception as exc:
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.MATERIAL_CONVERSION_FAILED,
            message="抽出結果を記事の素材へ整形する処理で例外が発生しました",
        ) from exc
