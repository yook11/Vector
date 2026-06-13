"""Stage 4/5 backfill から除外した target の現在状態 sentinel。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = [
    "AssessmentBackfillExclusion",
    "BackfillExclusionReason",
    "EmbeddingBackfillExclusion",
]


class BackfillExclusionReason(StrEnum):
    """backfill 除外理由。

    DB native ENUM は使わず、Python StrEnum + DB CHECK 制約で値を管理する。
    pipeline_events.outcome_code もこの値を参照し、監査コードとの SSoT を揃える。
    """

    ASSESSMENT_AGED_OUT = "backfill_assessment_aged_out"
    EMBEDDING_AGED_OUT = "backfill_embedding_aged_out"


class AssessmentBackfillExclusion(Base):
    """Stage 4 assessment の通常 backfill から除外した curation。"""

    __tablename__ = "assessment_backfill_exclusions"
    __table_args__ = (
        CheckConstraint(
            f"reason_code IN ('{BackfillExclusionReason.ASSESSMENT_AGED_OUT.value}')",
            name="ck_assessment_backfill_exclusions_reason_code",
        ),
    )

    curation_id: Mapped[int] = mapped_column(
        ForeignKey("article_curations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    excluded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class EmbeddingBackfillExclusion(Base):
    """Stage 5 embedding の通常 backfill から除外した analysis。"""

    __tablename__ = "embedding_backfill_exclusions"
    __table_args__ = (
        CheckConstraint(
            f"reason_code IN ('{BackfillExclusionReason.EMBEDDING_AGED_OUT.value}')",
            name="ck_embedding_backfill_exclusions_reason_code",
        ),
    )

    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyzed_articles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    excluded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
