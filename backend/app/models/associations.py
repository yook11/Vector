from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class NewsKeyword(SQLModel, table=True):
    __tablename__ = "news_keywords"
    __table_args__ = (
        UniqueConstraint("news_article_id", "keyword_id", name="uq_news_keyword"),
    )

    id: int | None = Field(default=None, primary_key=True)
    news_article_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("news_articles.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    keyword_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("keywords.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    # Relationships
    news_article: "NewsArticle | None" = Relationship(back_populates="keyword_links")
    keyword: "Keyword | None" = Relationship(back_populates="news_links")


# Resolve forward references
from app.models.keyword import Keyword  # noqa: E402, F811
from app.models.news import NewsArticle  # noqa: E402, F811

NewsKeyword.model_rebuild()
