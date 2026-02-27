from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, Relationship, SQLModel


class AnalysisResult(SQLModel, table=True):
    __tablename__ = "analyses"
    __table_args__ = (
        Index("idx_analyses_sentiment", "sentiment"),
        Index("idx_analyses_impact", "impact_score"),
    )

    id: int | None = Field(default=None, primary_key=True)
    news_article_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("news_articles.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        )
    )
    sentiment: str = Field(max_length=20, nullable=False)
    impact_score: int = Field(ge=1, le=10, nullable=False)
    reasoning: str | None = Field(default=None)
    ai_provider: str = Field(max_length=20, nullable=False)
    ai_model: str = Field(max_length=50, nullable=False)
    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    news_article: "NewsArticle" = Relationship(back_populates="analysis")
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
from app.models.investment_category import (  # noqa: E402
    AnalysisInvestmentCategory,  # noqa: F811
)
from app.models.news import NewsArticle  # noqa: E402, F811

AnalysisResult.model_rebuild()
AnalysisTranslation.model_rebuild()
