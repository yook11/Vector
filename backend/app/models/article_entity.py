"""記事から抽出されたエンティティ（企業・製品・技術）。"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_analysis import ArticleAnalysis


class EntityType(StrEnum):
    COMPANY = "company"
    PRODUCT = "product"
    TECHNOLOGY = "technology"


class ArticleEntity(Base):
    __tablename__ = "article_entities"
    __table_args__ = (
        CheckConstraint(
            "type IN ('company', 'product', 'technology')",
            name="ck_article_entities_type_valid",
        ),
        Index("ix_article_entities_name_type", "name", "type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_analysis_id: Mapped[int] = mapped_column(
        ForeignKey("article_analyses.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[EntityType] = mapped_column(String(20))

    # リレーション
    article_analysis: Mapped[ArticleAnalysis] = relationship(back_populates="entities")
