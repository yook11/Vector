"""全アプリケーションモデル共通の DeclarativeBase。

Metadata は共有: Base.metadata = SQLModel.metadata
これにより Alembic・init_db・テストが単一メタデータで全テーブルを参照する。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase
from sqlmodel import SQLModel

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

    metadata = SQLModel.metadata  # noqa: RUF012

    type_annotation_map = {  # noqa: RUF012
        CategorySlug: CategorySlugType,
        CategoryName: CategoryNameType,
        SafeUrl: SafeUrlType,
        SourceName: SourceNameType,
    }
