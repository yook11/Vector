from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlmodel import Field, Relationship, SQLModel


class ImpactLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ArticleAnalysis(SQLModel, table=True):
    __tablename__ = "article_analyses"
    __table_args__ = (
        UniqueConstraint("news_article_id", name="uq_article_analyses_news_article_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    news_article_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey(
                "news_articles.id",
                ondelete="CASCADE",
                name="fk_article_analyses_news_article_id",
            ),
            nullable=False,
        )
    )
    translated_title: str = Field(max_length=500, nullable=False)
    summary: str = Field(sa_column=Column(Text, nullable=False))
    impact_level: ImpactLevel = Field(sa_type=String(20), nullable=False)
    reasoning: str = Field(sa_column=Column(Text, nullable=False))
    ai_model: str = Field(max_length=100, nullable=False)
    analyzed_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        ),
    )
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(768), nullable=True),
    )
    embedding_model: str | None = Field(default=None, max_length=100)

    # Relationships
    news_article: "NewsArticle" = Relationship(back_populates="article_analysis")


# Resolve forward references
from app.models.news import NewsArticle  # noqa: E402, F811

ArticleAnalysis.model_rebuild()
