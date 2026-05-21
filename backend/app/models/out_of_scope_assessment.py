"""Stage 4 (Assessment) で out-of-scope と判定された curation の記録 (ORM)。

in_scope_assessments とは同一 curation に対して排他（DB トリガーで強制）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_curation import ArticleCuration


__all__ = ["OutOfScopeAssessment"]


class OutOfScopeAssessment(Base):
    """Stage 4 で out-of-scope と判定された curation の記録 (ORM)。"""

    __tablename__ = "out_of_scope_assessments"
    __table_args__ = (
        UniqueConstraint("curation_id", name="uq_out_of_scope_assessments_curation_id"),
        CheckConstraint(
            "translated_title != ''",
            name="ck_out_of_scope_assessments_translated_title_not_empty",
        ),
        CheckConstraint(
            "summary != ''",
            name="ck_out_of_scope_assessments_summary_not_empty",
        ),
        CheckConstraint(
            "investor_take != ''",
            name="ck_out_of_scope_assessments_investor_take_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    curation_id: Mapped[int] = mapped_column(
        ForeignKey("article_curations.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    investor_take: Mapped[str] = mapped_column(Text())
    # event-extraction PR 1 並列出力。InScopeAssessment と対称化
    # (out-of-scope と判定された記事の events も検証用途で保持)。
    # NULL = PR 1 デプロイ前の旧行、[] = AI が events を返さなかった新行、
    # values = AI 抽出済み新行。
    events: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    curation: Mapped[ArticleCuration] = relationship(
        back_populates="out_of_scope_assessment"
    )
