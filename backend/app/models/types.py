"""SQLAlchemy TypeDecorators for domain value objects.

Each TypeDecorator converts between a VO (Python side) and a plain string (DB side).
Raw str is accepted and validated through the VO constructor (no bypass).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.domain.category import CategoryName, CategorySlug
from app.domain.keyword import KeywordName


class CategorySlugType(TypeDecorator[CategorySlug]):
    """CategorySlug <-> VARCHAR(50)."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, CategorySlug):
            return value.root
        if isinstance(value, str):
            return CategorySlug(value).root
        raise TypeError(f"Expected CategorySlug or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> CategorySlug | None:
        if value is None:
            return None
        return CategorySlug(value)


class CategoryNameType(TypeDecorator[CategoryName]):
    """CategoryName <-> VARCHAR(50)."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, CategoryName):
            return value.root
        if isinstance(value, str):
            return CategoryName(value).root
        raise TypeError(f"Expected CategoryName or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> CategoryName | None:
        if value is None:
            return None
        return CategoryName(value)


class KeywordNameType(TypeDecorator[KeywordName]):
    """KeywordName <-> VARCHAR(100)."""

    impl = String(100)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, KeywordName):
            return value.root
        if isinstance(value, str):
            return KeywordName(value).root
        raise TypeError(f"Expected KeywordName or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> KeywordName | None:
        if value is None:
            return None
        return KeywordName(value)
