"""記事の Stage 3 curation 結果 (翻訳タイトル + 事実ベース要約)。

Stage 4 (Assessment) が InScope か OutOfScope に振れる前の、原文を読み翻訳・
要約して signal として残した成果物。in_scope_assessment / out_of_scope_assessment
のいずれか一方 (排他) を持ちうる。CurationNoise (curation_noises) と article 単位で
排他関係を持つ (DB trigger で構造的に保証)。

``extracted_at`` 列名は Stage 3 出力時刻を表す事実列として据え置く
(curation rename 配下でも変えない)。
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


class ArticleCuration(Base):
    __tablename__ = "article_curations"
    __table_args__ = (
        UniqueConstraint("article_id", name="uq_article_curations_article_id"),
        CheckConstraint(
            "translated_title != ''",
            name="ck_article_curations_translated_title_not_empty",
        ),
        CheckConstraint(
            "summary != ''",
            name="ck_article_curations_summary_not_empty",
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
    article: Mapped[Article] = relationship(back_populates="curation")
    in_scope_assessment: Mapped[InScopeAssessment | None] = relationship(
        back_populates="curation", uselist=False
    )
    out_of_scope_assessment: Mapped[OutOfScopeAssessment | None] = relationship(
        back_populates="curation", uselist=False
    )
