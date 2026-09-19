"""Pydantic 検証例外から、入力値を除いた原因情報を取り出す。"""

from __future__ import annotations

from typing import get_args

from pydantic import ValidationError
from pydantic_core import ErrorType

_VALIDATION_TYPES = frozenset(get_args(ErrorType))


def extract_validation_message(exc: ValidationError) -> str:
    """入力値を含む説明や位置を除き、検証件数と標準分類を返す。"""
    if exc.error_count() > 100:
        return f"Validation failed ({exc.error_count()} errors)"
    # msg / loc / title / custom type にも入力が入り得るため標準分類だけ残す。
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    kinds = sorted(
        {
            e["type"] if e["type"] in _VALIDATION_TYPES else "custom_error"
            for e in errors
        }
    )
    return f"Validation failed ({exc.error_count()} errors): {', '.join(kinds)}"
