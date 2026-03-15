"""Text sanitization utilities for cleaning fetched content."""

import html
import re
from urllib.parse import urlparse

_TAG_RE = re.compile(r"<[^>]+>")

# --- XSS対策: URLスキームのホワイトリスト ---
#
# 外部データに含まれるURLのスキームを検証する共通ユーティリティ。
# javascript:, data:, vbscript: 等の危険なスキームを排除し、
# http/https のみを許可する。
#
# 2つの関数を用途別に提供:
#   is_safe_url()         — bool を返す（フェッチャー等の条件分岐向け）
#   validate_url_scheme() — 不正時に ValueError を raise（Pydantic バリデーター向け）
_SAFE_URL_SCHEMES = {"http", "https"}


def is_safe_url(url: str) -> bool:
    """Check if a URL has a safe scheme (http or https only).

    Used by fetchers to filter out articles with dangerous URL schemes
    before saving to the database.
    """
    try:
        parsed = urlparse(url)
        return parsed.scheme in _SAFE_URL_SCHEMES and bool(parsed.netloc)
    except Exception:
        return False


def validate_url_scheme(url: str, field_name: str = "url") -> str:
    """Validate that a URL uses http or https scheme.

    Raises ValueError for Pydantic field_validator compatibility.
    Used in Pydantic schemas to reject unsafe URLs at the API boundary.
    """
    if not is_safe_url(url):
        raise ValueError(f"{field_name} must be a valid http or https URL")
    return url


def strip_html_tags(text: str | None) -> str | None:
    """Strip HTML tags and decode HTML entities from text.

    Returns None if input is None.
    """
    if text is None:
        return None
    cleaned = _TAG_RE.sub("", text)
    return html.unescape(cleaned).strip()
