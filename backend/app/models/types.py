"""ドメイン値オブジェクト向けの SQLAlchemy TypeDecorator 群。

各 TypeDecorator は VO（Python 側）とプレーン文字列（DB 側）を相互変換する。
生の str も受け付けるが、VO コンストラクタを通して検証される（バイパス不可）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.domain.category import CategoryName, CategorySlug
from app.domain.news_source import SourceName
from app.domain.safe_url import SafeUrl
from app.domain.topic import TopicName


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


class TopicNameType(TypeDecorator[TopicName]):
    """TopicName <-> VARCHAR(100)."""

    impl = String(100)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, TopicName):
            return value.root
        if isinstance(value, str):
            return TopicName(value).root
        raise TypeError(f"Expected TopicName or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> TopicName | None:
        if value is None:
            return None
        return TopicName(value)


class SourceNameType(TypeDecorator[SourceName]):
    """SourceName <-> VARCHAR(50)."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, SourceName):
            return value.root
        if isinstance(value, str):
            return SourceName(value).root
        raise TypeError(f"Expected SourceName or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> SourceName | None:
        if value is None:
            return None
        return SourceName(value)


class SafeUrlType(TypeDecorator[SafeUrl]):
    """SafeUrl <-> VARCHAR(2048)."""

    impl = String(2048)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, SafeUrl):
            return value.root
        if isinstance(value, str):
            return SafeUrl(value).root
        raise TypeError(f"Expected SafeUrl or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> SafeUrl | None:
        if value is None:
            return None
        return SafeUrl(value)
