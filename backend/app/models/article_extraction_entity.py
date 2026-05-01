"""Stage 1 観察台帳の 1 行 (記事から抽出された 1 entity)。

Phase 1B α-1 で旧 ``article_entities`` テーブルから clean break で置換。
変更点:

- ``surface`` (旧 ``name``): EntitySurface (= EntityName) で casing を保持
- ``raw_type`` (旧 ``type``): 新 EntityRawType で casing を保持 + lower 化しない
  (β の canonical_type と衝突させないための設計)
- ``position``: AI 出力順を保存 (新規)
- PK は BigInteger (再抽出運用で行数が累積するため余裕を持たせる)
- FK は Integer (親 ``article_extractions.id`` も Integer)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_extraction import ArticleExtraction


class ArticleExtractionEntity(Base):
    __tablename__ = "article_extraction_entities"
    __table_args__ = (
        CheckConstraint("surface != ''", name="ck_aee_surface_not_empty"),
        CheckConstraint("raw_type != ''", name="ck_aee_raw_type_not_empty"),
        Index("ix_article_extraction_entities_extraction_id", "extraction_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("article_extractions.id", ondelete="CASCADE")
    )
    surface: Mapped[EntitySurface] = mapped_column()
    raw_type: Mapped[EntityRawType] = mapped_column()
    position: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    extraction: Mapped[ArticleExtraction] = relationship(back_populates="entities")
