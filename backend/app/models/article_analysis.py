from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import HALFVEC
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

from app.models.article_entity import ArticleEntity
from app.models.base import Base
from app.utils.sanitize import strip_html_tags

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.topic import Topic
    from app.models.watchlist_entry import WatchlistEntry


class ImpactLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ArticleAnalysis(Base):
    __tablename__ = "article_analyses"
    __table_args__ = (
        UniqueConstraint("article_id", name="uq_article_analyses_article_id"),
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
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    impact_level: Mapped[ImpactLevel | None] = mapped_column(String(20))
    reasoning: Mapped[str | None] = mapped_column(Text())
    ai_model: Mapped[str] = mapped_column(String(100))
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(768))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    topic_id: Mapped[int | None] = mapped_column(
        ForeignKey("topics.id", ondelete="RESTRICT"), index=True
    )

    # リレーション
    article: Mapped[Article] = relationship(back_populates="article_analysis")
    topic: Mapped[Topic | None] = relationship()
    watchlist_entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="article_analysis"
    )
    entities: Mapped[list[ArticleEntity]] = relationship(
        back_populates="article_analysis", cascade="all, delete-orphan"
    )

    @classmethod
    def from_extraction(
        cls,
        *,
        article_id: int,
        title_ja: str,
        summary_ja: str,
        entities: list[tuple[str, str]],
        model_name: str,
    ) -> ArticleAnalysis:
        """Stage 1 の抽出結果から分析オブジェクトを構築する。

        サニタイズと Entity の組み立てはモデルの不変条件として内部で処理する。
        """
        return cls(
            article_id=article_id,
            translated_title=strip_html_tags(title_ja) or "",
            summary=strip_html_tags(summary_ja) or "",
            ai_model=model_name,
            entities=[ArticleEntity(name=name, type=etype) for name, etype in entities],
        )
