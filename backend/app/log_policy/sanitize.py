"""調査のために残したい値を、項目の意味に沿って必要な部分だけに絞る処理を項目名から選ぶ。"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit


def sanitize_article_url(url: str) -> str:
    """記事URLを分解し、userinfo とフラグメントを除いた部分から組み立て直す。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[unsupported]"
    host_and_port = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host_and_port, parts.path, parts.query, ""))


_FIELD_SANITIZERS: dict[str, Callable[[str], str]] = {
    "canonical_url": sanitize_article_url,
    "source_url": sanitize_article_url,
}


def sanitize_field_value(field_name: str, value: object) -> str:
    """対応表にある項目の値を処理し、処理のない項目名や入力型が合わない値は固定マーカーにする。"""
    sanitizer = _FIELD_SANITIZERS.get(field_name)
    if sanitizer is None or type(value) is not str:
        return "[unsupported]"
    return sanitizer(value)
