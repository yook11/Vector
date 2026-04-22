"""記事からの Stage 1 抽出結果（翻訳タイトル・要約・エンティティ）。

Stage 2（分類）が Classified か OutOfScope に振れる前の、
原文から抽出した事実ベースの成果物。
entities を子として持ち、analysis / rejection のいずれか一方（排他）を持ちうる。
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

from app.models.article_entity import ArticleEntity
from app.models.base import Base
from app.utils.sanitize import strip_html_tags

if TYPE_CHECKING:
    from app.analysis.extraction.schema import ExtractionResponse
    from app.models.article import Article
    from app.models.article_analysis import ArticleAnalysis
    from app.models.article_rejection import ArticleRejection


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
        CheckConstraint(
            "ai_model != ''",
            name="ck_article_extractions_ai_model_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    ai_model: Mapped[str] = mapped_column(String(100))
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    article: Mapped[Article] = relationship(back_populates="extraction")
    entities: Mapped[list[ArticleEntity]] = relationship(
        back_populates="extraction", cascade="all, delete-orphan"
    )
    analysis: Mapped[ArticleAnalysis | None] = relationship(
        back_populates="extraction", uselist=False
    )
    rejection: Mapped[ArticleRejection | None] = relationship(
        back_populates="extraction", uselist=False
    )

    @classmethod
    def from_extraction_response(
        cls,
        *,
        article_id: int,
        response: ExtractionResponse,
        model_name: str,
    ) -> ArticleExtraction:
        """Stage 1 の AI レスポンスから抽出オブジェクトを構築する。

        サニタイズと Entity の組み立てはモデルの不変条件として内部で処理する。
        """
        return cls(
            article_id=article_id,
            translated_title=strip_html_tags(response.title_ja) or "",
            summary=strip_html_tags(response.summary_ja) or "",
            ai_model=model_name,
            entities=[
                ArticleEntity(name=e.name.root, type=e.type.root)
                for e in response.entities
            ],
        )
