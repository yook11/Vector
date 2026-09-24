"""調査のために残したい値を、項目の意味に沿って必要な部分だけに絞る処理を項目名から選ぶ。"""

from __future__ import annotations

from collections.abc import Callable

_FIELD_SANITIZERS: dict[str, Callable[[str], str]] = {}


def sanitize_field_value(field_name: str, value: object) -> str:
    """対応表にある項目の値を処理し、入力型が合わなければ固定マーカーを返す。"""
    sanitizer = _FIELD_SANITIZERS[field_name]
    if type(value) is not str:
        return "[unsupported]"
    return sanitizer(value)
