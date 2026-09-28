from __future__ import annotations

from typing import Final

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 外から来る slug (パス・クエリ) の検証もこの定数を使い、DB 制約と形式を揃える。
CATEGORY_SLUG_PATTERN: Final = r"^[a-z0-9][a-z0-9_]{0,49}$"


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(
            f"slug ~ '{CATEGORY_SLUG_PATTERN}'",
            name="ck_categories_slug_format",
        ),
        CheckConstraint(
            "char_length(name) >= 1",
            name="ck_categories_name_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
