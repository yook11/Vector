from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_extraction import ArticleExtraction
    from app.models.topic import Topic
    from app.models.watchlist_entry import WatchlistEntry


__all__ = ["ArticleAnalysis"]


class ArticleAnalysis(Base):
    """Stage 2 で Classified と判定された extraction の分析結果。

    translated_title / summary は extractions から複製保持し、analysis 単独で
    「ユーザーに見せる分析結果」として自己完結する。
    """

    __tablename__ = "article_analyses"
    __table_args__ = (
        UniqueConstraint("extraction_id", name="uq_article_analyses_extraction_id"),
        CheckConstraint(
            "translated_title != ''",
            name="ck_article_analyses_translated_title_not_empty",
        ),
        CheckConstraint(
            "summary != ''",
            name="ck_article_analyses_summary_not_empty",
        ),
        CheckConstraint(
            "ai_model != ''",
            name="ck_article_analyses_ai_model_not_empty",
        ),
        CheckConstraint(
            "investor_take != ''",
            name="ck_article_analyses_investor_take_not_empty",
        ),
        # サイドバーの直近 24 時間集計クエリ向けの複合インデックス
        Index(
            "ix_article_analyses_topic_id_analyzed_at",
            "topic_id",
            "analyzed_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("article_extractions.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    investor_take: Mapped[str] = mapped_column(Text())
    ai_model: Mapped[str] = mapped_column(String(100))
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(768))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="RESTRICT"), index=True
    )

    # リレーション
    extraction: Mapped[ArticleExtraction] = relationship(back_populates="analysis")
    topic: Mapped[Topic] = relationship()
    watchlist_entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="article_analysis"
    )

    @classmethod
    def from_classification(
        cls,
        *,
        extraction_id: int,
        translated_title: str,
        summary: str,
        topic_id: int,
        investor_take: str,
        model_name: str,
    ) -> ArticleAnalysis:
        """Stage 2 の分類結果から分析オブジェクトを構築する。

        translated_title / summary は extraction から複製する（自己完結の保証）。
        呼び出し側 (Service) が Extraction ドメイン Entity からこれらを渡す。
        """
        return cls(
            extraction_id=extraction_id,
            translated_title=translated_title,
            summary=summary,
            topic_id=topic_id,
            investor_take=investor_take,
            ai_model=model_name,
        )
