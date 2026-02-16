from datetime import UTC, datetime

from sqlalchemy import Index
from sqlmodel import Field, Relationship, SQLModel


class NewsArticle(SQLModel, table=True):
    __tablename__ = "news_articles"
    __table_args__ = (
        Index("idx_news_published", "published_at", postgresql_using="btree"),
        Index("idx_news_fetched", "fetched_at", postgresql_using="btree"),
    )

    id: int | None = Field(default=None, primary_key=True)
    title_original: str = Field(max_length=500, nullable=False)
    description_original: str | None = Field(default=None)
    url: str = Field(max_length=2048, unique=True, nullable=False, index=True)
    source: str = Field(max_length=100, nullable=False)
    published_at: datetime | None = Field(default=None)
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), nullable=False
    )

    # Relationships
    analysis: "AnalysisResult | None" = Relationship(
        back_populates="news_article",
        sa_relationship_kwargs={"uselist": False},
    )
    keyword_links: list["NewsKeyword"] = Relationship(back_populates="news_article")


# Resolve forward references
from app.models.analysis import AnalysisResult  # noqa: E402, F811
from app.models.associations import NewsKeyword  # noqa: E402, F811

NewsArticle.model_rebuild()
