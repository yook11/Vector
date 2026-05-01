"""記事からの Stage 1 抽出結果（翻訳タイトル・要約・エンティティ）。

Stage 2（分類）が Classified か OutOfScope に振れる前の、
原文から抽出した事実ベースの成果物。
entities を子として持ち、analysis / rejection のいずれか一方（排他）を持ちうる。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.article_extraction_entity import ArticleExtractionEntity
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.article_analysis import ArticleAnalysis
    from app.models.article_rejection import ArticleRejection


class ArticleExtraction(Base):
    __tablename__ = "article_extractions"
    __table_args__ = (
        UniqueConstraint("article_id", name="uq_article_extractions_article_id"),
        CheckConstraint(
            "translated_title != ''",
            name="ck_article_extractions_translated_title_not_empty",
        ),
        CheckConstraint(
            "summary != ''",
            name="ck_article_extractions_summary_not_empty",
        ),
        CheckConstraint(
            "ai_model != ''",
            name="ck_article_extractions_ai_model_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    ai_model: Mapped[str] = mapped_column(String(100))
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    article: Mapped[Article] = relationship(back_populates="extraction")
    entities: Mapped[list[ArticleExtractionEntity]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        order_by="ArticleExtractionEntity.position",
    )
    analysis: Mapped[ArticleAnalysis | None] = relationship(
        back_populates="extraction", uselist=False
    )
    rejection: Mapped[ArticleRejection | None] = relationship(
        back_populates="extraction", uselist=False
    )
