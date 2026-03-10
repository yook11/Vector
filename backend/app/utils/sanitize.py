"""Text sanitization utilities for cleaning fetched content."""

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html_tags(text: str | None) -> str | None:
    """Strip HTML tags and decode HTML entities from text.

    Returns None if input is None.
    """
    if text is None:
        return None
    cleaned = _TAG_RE.sub("", text)
    return html.unescape(cleaned).strip()
