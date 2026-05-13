"""ドメイン値オブジェクト向けの SQLAlchemy TypeDecorator 群。

各 TypeDecorator は VO（Python 側）とプレーン文字列（DB 側）を相互変換する。
生の str も受け付けるが、VO コンストラクタを通して検証される（バイパス不可）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.analysis.domain.value_objects.entity import (
    EntityName,
    EntityRawType,
    EntityType,
)
from app.analysis.domain.value_objects.topic import TopicName
from app.collection.domain.value_objects.source import SourceName
from app.domain.category import CategoryName, CategorySlug
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.safe_url import SafeUrl


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


class EntityNameType(TypeDecorator[EntityName]):
    """EntityName <-> VARCHAR(200)."""

    impl = String(200)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, EntityName):
            return value.root
        if isinstance(value, str):
            return EntityName(value).root
        raise TypeError(f"Expected EntityName or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> EntityName | None:
        if value is None:
            return None
        return EntityName(value)


class EntityTypeType(TypeDecorator[EntityType]):
    """EntityType <-> VARCHAR(50)."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, EntityType):
            return value.root
        if isinstance(value, str):
            return EntityType(value).root
        raise TypeError(f"Expected EntityType or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> EntityType | None:
        if value is None:
            return None
        return EntityType(value)


class EntityRawTypeType(TypeDecorator[EntityRawType]):
    """EntityRawType <-> VARCHAR(30).

    Stage 1 観察用 type ラベル (casing 保持、lower 化しない、match_key 持たない)
    のための adapter。``EntityType`` (lower 化する) と独立した型として扱う。
    """

    impl = String(30)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, EntityRawType):
            return value.root
        if isinstance(value, str):
            return EntityRawType(value).root
        raise TypeError(f"Expected EntityRawType or str, got {type(value).__name__}")

    def process_result_value(
        self, value: Any, dialect: Dialect
    ) -> EntityRawType | None:
        if value is None:
            return None
        return EntityRawType(value)


class SafeUrlType(TypeDecorator[SafeUrl]):
    """SafeUrl <-> VARCHAR(2048).

    ``CanonicalArticleUrl`` も同じ列に書き込めるように bind 側で受容する。
    canonical 値は SafeUrl の不変条件を満たすため、DB 側の物理表現は変わらず、
    Repository signature を ``CanonicalArticleUrl`` に上げても ORM 列の型は
    SafeUrl のままで透過処理できる (記事 identity の SSoT を型に寄せる目的)。
    """

    impl = String(2048)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, CanonicalArticleUrl):
            return value.root
        if isinstance(value, SafeUrl):
            return value.root
        if isinstance(value, str):
            return SafeUrl(value).root
        raise TypeError(
            f"Expected SafeUrl, CanonicalArticleUrl or str, got {type(value).__name__}"
        )

    def process_result_value(self, value: Any, dialect: Dialect) -> SafeUrl | None:
        if value is None:
            return None
        return SafeUrl(value)
