"""記事から抽出されたエンティティ。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_extraction import ArticleExtraction


class ArticleEntity(Base):
    __tablename__ = "article_entities"
    __table_args__ = (Index("ix_article_entities_name_type", "name", "type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_extraction_id: Mapped[int] = mapped_column(
        ForeignKey("article_extractions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(50))

    # リレーション
    extraction: Mapped[ArticleExtraction] = relationship(back_populates="entities")
