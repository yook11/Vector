"""Stage 4 (Assessment) で out-of-scope と判定された extraction の記録 (ORM)。

article_analyses (in_scope_assessments) とは同一 extraction に対して排他
（DB トリガーで強制）。

注: ``__tablename__`` は旧名 ``article_rejections`` のまま据え置き。DB rename
は PR3.5-d.1 の Alembic migration で行う。
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
    from app.models.article_extraction import ArticleExtraction


__all__ = ["OutOfScopeAssessment"]


class OutOfScopeAssessment(Base):
    """Stage 4 で out-of-scope と判定された extraction の記録 (ORM)。"""

    # PR3.5-d.0: 旧名 "article_rejections" のまま据え置き。PR3.5-d.1 で
    # "out_of_scope_assessments" に rename 予定。
    __tablename__ = "article_rejections"
    __table_args__ = (
        UniqueConstraint("extraction_id", name="uq_article_rejections_extraction_id"),
        CheckConstraint(
            "investor_take != ''",
            name="ck_article_rejections_investor_take_not_empty",
        ),
        CheckConstraint(
            "ai_model != ''",
            name="ck_article_rejections_ai_model_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("article_extractions.id", ondelete="CASCADE"),
    )
    investor_take: Mapped[str] = mapped_column(Text())
    ai_model: Mapped[str] = mapped_column(String(100))
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    extraction: Mapped[ArticleExtraction] = relationship(
        back_populates="out_of_scope_assessment"
    )
