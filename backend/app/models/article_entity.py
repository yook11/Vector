"""旧テーブル ``article_entities`` の ORM (Phase 1B α-1 で deprecated)。

Phase 1B α-1 で ``article_extraction_entities`` に置換された。本 ORM はテーブルが
DROP されるまで (l9 migration) metadata に残しておくが、``ArticleExtraction``
からの relationship は ``ArticleExtractionEntity`` に切り替わっている。新規
書き込み・読み出しは新 ORM 経由で行うこと。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.analysis.domain.value_objects.entity import EntityName, EntityType
from app.models.base import Base


class ArticleEntity(Base):
    __tablename__ = "article_entities"
    __table_args__ = (Index("ix_article_entities_name_type", "name", "type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_extraction_id: Mapped[int] = mapped_column(
        ForeignKey("article_extractions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[EntityName] = mapped_column()
    type: Mapped[EntityType] = mapped_column()
