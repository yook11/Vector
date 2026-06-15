"""Stage 3 (curation) で noise 判定された記事の永続化 ORM モデル。

``article_curations`` (signal 側) と排他関係を DB トリガー対称ペア
(``t2_curation_table_rename`` migration で再作成) で構造的に強制する。
1 article に対し ``article_curations`` または ``curation_noises`` のどちらか
一方しか存在できない。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
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
    from app.models.analyzable_article_record import AnalyzableArticleRecord

__all__ = ["CurationNoise"]


class CurationNoise(Base):
    """Stage 3 で ``relevance="noise"`` と判定された記事の記録。"""

    __tablename__ = "curation_noises"
    __table_args__ = (
        UniqueConstraint(
            "analyzable_article_id",
            name="uq_curation_noises_analyzable_article_id",
        ),
        CheckConstraint(
            "translated_title <> ''",
            name="ck_curation_noises_translated_title_not_empty",
        ),
        CheckConstraint(
            "summary <> ''",
            name="ck_curation_noises_summary_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    analyzable_article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("analyzable_articles.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    analyzable_article: Mapped[AnalyzableArticleRecord] = relationship(
        back_populates="curation_noise"
    )
