"""Stage 4 (Assessment) で out-of-scope と判定された extraction の記録 (ORM)。

in_scope_assessments とは同一 extraction に対して排他（DB トリガーで強制）。
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

    __tablename__ = "out_of_scope_assessments"
    __table_args__ = (
        UniqueConstraint(
            "extraction_id", name="uq_out_of_scope_assessments_extraction_id"
        ),
        CheckConstraint(
            "investor_take != ''",
            name="ck_out_of_scope_assessments_investor_take_not_empty",
        ),
        CheckConstraint(
            "ai_model != ''",
            name="ck_out_of_scope_assessments_ai_model_not_empty",
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
