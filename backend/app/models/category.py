from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.category import CategoryName, CategorySlug
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.topic import Topic


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9_]{0,49}$'",
            name="ck_categories_slug_format",
        ),
        CheckConstraint(
            "char_length(name) >= 1",
            name="ck_categories_name_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[CategorySlug] = mapped_column(unique=True, index=True)
    name: Mapped[CategoryName] = mapped_column(unique=True)

    # リレーション（同一 Base のため OK）
    topics: Mapped[list[Topic]] = relationship(back_populates="category")
