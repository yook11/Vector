from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import JSONB
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
    title_ja: str = Field(max_length=500, nullable=False)
    summary_ja: str = Field(nullable=False)
    sentiment: str = Field(max_length=20, nullable=False)
    impact_score: int = Field(ge=1, le=10, nullable=False)
    key_topics: list[str] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
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


# Resolve forward reference
from app.models.news import NewsArticle  # noqa: E402, F811

AnalysisResult.model_rebuild()
