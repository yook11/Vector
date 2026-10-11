"""取得済みHTMLから素材を抽出し、新経路用の失敗を伝える。"""

import re
from datetime import UTC, datetime
from enum import StrEnum

import trafilatura
from trafilatura.settings import Document as TrafilaturaDocument

from app.collection.article_completion.content import RawResponse, ScrapedContent
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
    """HTMLを抽出素材に変換し、失敗した処理の理由と元の原因を伝える。"""
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
        return ScrapedContent.from_extraction(
            raw_title=result.title,
            stripped_body=result.text if result.text is not None else "",
            raw_date=result.date,
        )
    except HtmlExtractionError:
        raise
    except Exception as exc:
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.MATERIAL_CONVERSION_FAILED,
            message="抽出結果を記事の素材へ整形する処理で例外が発生しました",
        ) from exc
