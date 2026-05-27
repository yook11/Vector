"""全アプリケーションモデル共通の DeclarativeBase。

Alembic / init_db / テストはすべて ``Base.metadata`` を target metadata に
取り、ここに登録されたテーブルを単一メタデータとして参照する。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase

from app.collection.sources.source_name import SourceName
from app.models.types import (
    CategoryNameType,
    CategorySlugType,
    SafeUrlType,
    SourceNameType,
)
from app.models.value_objects.category import CategoryName, CategorySlug
from app.shared.security.safe_url import SafeUrl


class Base(DeclarativeBase):
    """VO の type_annotation_map を備えた共通 DeclarativeBase。"""

    type_annotation_map = {  # noqa: RUF012
        CategorySlug: CategorySlugType,
        CategoryName: CategoryNameType,
        SafeUrl: SafeUrlType,
        SourceName: SourceNameType,
    }
