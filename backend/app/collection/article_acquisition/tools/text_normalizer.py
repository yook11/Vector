"""テキスト正規化道具 — HTML 平文化と空白整形。"""

from __future__ import annotations

import re

from app.shared.text import normalize_text

_BR_RE = re.compile(r"<br\s*/?\s*>", re.IGNORECASE)
_CLOSING_P_RE = re.compile(r"</\s*p\s*>", re.IGNORECASE)


def html_to_plain_text(html: str) -> str:
    """HTML を段落改行付きで平文化する (``<br>``→改行 / ``</p>``→空行)。"""
    converted = _BR_RE.sub("\n", html)
    converted = _CLOSING_P_RE.sub("\n\n", converted)
    return normalize_text(converted)
