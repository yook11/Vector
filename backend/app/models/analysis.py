from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, Relationship, SQLModel


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class AnalysisResult(SQLModel, table=True):
    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint(
            "news_article_id", "ai_model_id", name="uq_analyses_article_model"
        ),
        Index("idx_analyses_sentiment", "sentiment"),
        Index("idx_analyses_impact", "impact_score"),
        Index("idx_analyses_ai_model_id", "ai_model_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    news_article_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("news_articles.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    ai_model_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("ai_models.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    sentiment: Sentiment = Field(sa_type=String(20), nullable=False)
    impact_score: int = Field(ge=1, le=10, nullable=False)
    reasoning: str | None = Field(default=None)
    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    news_article: "NewsArticle" = Relationship(back_populates="analyses")
    ai_model: "AIModel" = Relationship(back_populates="analyses")
    category_links: list["AnalysisInvestmentCategory"] = Relationship(
        back_populates="analysis"
    )
    translations: list["AnalysisTranslation"] = Relationship(back_populates="analysis")


class AnalysisTranslation(SQLModel, table=True):
    __tablename__ = "analysis_translations"
    __table_args__ = (
        UniqueConstraint("analysis_id", "locale", name="uq_analysis_locale"),
    )

    id: int | None = Field(default=None, primary_key=True)
    analysis_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    locale: str = Field(max_length=10, nullable=False)
    title: str = Field(max_length=500, nullable=False)
    summary: str = Field(sa_column=Column(Text, nullable=False))

    # Relationships
    analysis: AnalysisResult = Relationship(back_populates="translations")


# Resolve forward references
from app.models.ai_model import AIModel  # noqa: E402, F811
from app.models.investment_category import (  # noqa: E402
    AnalysisInvestmentCategory,  # noqa: F811
)
from app.models.news import NewsArticle  # noqa: E402, F811

AnalysisResult.model_rebuild()
AnalysisTranslation.model_rebuild()
