"""文字列中のキー付き値を、確定したmaskに従って伏せる。"""

import re

from app.log_policy.base import normalize_key

# 引用符付きの値は閉じ忘れも末尾まで伏せ、エスケープされた引用符で切らない。
_ASSIGNMENT = re.compile(r"(?<![\w-])(?P<key>[A-Za-z_][A-Za-z0-9_-]*)['\"]?\s*[:=]\s*")
_QUOTED_VALUE = re.compile(
    r"\"(?:\\[\s\S]?|[^\"\\])*(?:\"|$)|'(?:\\[\s\S]?|[^'\\])*(?:'|$)"
)
_UNQUOTED_VALUE = re.compile(r"[^\s,}&;]+")
_HEADER_VALUE = re.compile(r"[^\r\n}]+")
_HEADER_KEYS = frozenset(
    {"authorization", "proxy_authorization", "cookie", "set_cookie"}
)
_CLOSING_BRACKETS = {"[": "]", "{": "}", "(": ")"}


def _container_value_end(text: str, start: int) -> int:
    """括弧と引用符から値の終端を求め、確定できなければ末尾まで保護する。"""
    closing_brackets = [_CLOSING_BRACKETS[text[start]]]
    position = start + 1
    while position < len(text):
        char = text[position]
        quoted_value = _QUOTED_VALUE.match(text, position)
        if quoted_value is not None:
            position = quoted_value.end()
            continue
        if char in _CLOSING_BRACKETS:
            closing_brackets.append(_CLOSING_BRACKETS[char])
        elif char in "]})":
            if char != closing_brackets.pop():
                return len(text)
            if not closing_brackets:
                return position + 1
        position += 1
    return len(text)


def mask_assignments(text: str, mask: frozenset[str]) -> str:
    """対象キーに対応する値を内容によらず伏せ、キー名と周囲の文を残す。"""
    parts: list[str] = []
    end = 0
    for match in _ASSIGNMENT.finditer(text):
        if match.start() < end:
            continue
        key = normalize_key(match["key"])
        if key not in mask:
            continue
        start = match.end()
        if (
            key not in _HEADER_KEYS
            and start < len(text)
            and text[start] in _CLOSING_BRACKETS
        ):
            value_end = _container_value_end(text, start)
        else:
            pattern = _HEADER_VALUE if key in _HEADER_KEYS else _UNQUOTED_VALUE
            value_match = _QUOTED_VALUE.match(text, start) or pattern.match(text, start)
            if value_match is None:
                continue
            value_end = value_match.end()
        parts.extend((text[end:start], "***"))
        end = value_end
    parts.append(text[end:])
    return "".join(parts)
