from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_analysis import ArticleAnalysis
    from app.models.keyword import Keyword


class ArticleKeyword(Base):
    __tablename__ = "article_keywords"

    article_analysis_id: Mapped[int] = mapped_column(
        ForeignKey("article_analyses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    keyword_id: Mapped[int] = mapped_column(
        ForeignKey("keywords.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # リレーション
    keyword: Mapped[Keyword] = relationship(back_populates="article_keywords")
    article_analysis: Mapped[ArticleAnalysis] = relationship(
        back_populates="article_keywords"
    )
