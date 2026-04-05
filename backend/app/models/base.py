"""DeclarativeBase shared by all application models.

Metadata is shared: Base.metadata = SQLModel.metadata
so that Alembic, init_db, and tests see all tables in one metadata.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase
from sqlmodel import SQLModel

from app.domain.category import CategoryName, CategorySlug
from app.domain.keyword import KeywordName
from app.domain.news_source import SourceName
from app.domain.safe_url import SafeUrl
from app.models.types import (
    CategoryNameType,
    CategorySlugType,
    KeywordNameType,
    SafeUrlType,
    SourceNameType,
)


class Base(DeclarativeBase):
    """Shared DeclarativeBase with VO type_annotation_map."""

    metadata = SQLModel.metadata  # noqa: RUF012

    type_annotation_map = {  # noqa: RUF012
        CategorySlug: CategorySlugType,
        CategoryName: CategoryNameType,
        KeywordName: KeywordNameType,
        SafeUrl: SafeUrlType,
        SourceName: SourceNameType,
    }
