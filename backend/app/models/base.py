"""全アプリケーションモデル共通の DeclarativeBase。

Metadata は共有: Base.metadata = SQLModel.metadata
これにより Alembic・init_db・テストが単一メタデータで全テーブルを参照する。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase
from sqlmodel import SQLModel

from app.domain.category import CategoryName, CategorySlug
from app.domain.news_source import SourceName
from app.domain.safe_url import SafeUrl
from app.domain.topic import TopicName
from app.models.types import (
    CategoryNameType,
    CategorySlugType,
    SafeUrlType,
    SourceNameType,
    TopicNameType,
)


class Base(DeclarativeBase):
    """VO の type_annotation_map を備えた共通 DeclarativeBase。"""

    metadata = SQLModel.metadata  # noqa: RUF012

    type_annotation_map = {  # noqa: RUF012
        CategorySlug: CategorySlugType,
        CategoryName: CategoryNameType,
        TopicName: TopicNameType,
        SafeUrl: SafeUrlType,
        SourceName: SourceNameType,
    }
