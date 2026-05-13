"""記事からの Stage 3 抽出結果（翻訳タイトル・要約）。

Stage 4 (Assessment) が InScope か OutOfScope に振れる前の、
原文から抽出した事実ベースの成果物。in_scope_assessment /
out_of_scope_assessment のいずれか一方（排他）を持ちうる。
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

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.in_scope_assessment import InScopeAssessment
    from app.models.out_of_scope_assessment import OutOfScopeAssessment


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
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    article: Mapped[Article] = relationship(back_populates="extraction")
    in_scope_assessment: Mapped[InScopeAssessment | None] = relationship(
        back_populates="extraction", uselist=False
    )
    out_of_scope_assessment: Mapped[OutOfScopeAssessment | None] = relationship(
        back_populates="extraction", uselist=False
    )
