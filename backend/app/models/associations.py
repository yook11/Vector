from sqlalchemy import Column, ForeignKey, Integer
from sqlmodel import Field, Relationship, SQLModel


class ArticleKeyword(SQLModel, table=True):
    __tablename__ = "article_keywords"

    news_article_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("news_articles.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    keyword_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("keywords.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )

    # Relationships
    news_article: "NewsArticle" = Relationship(back_populates="article_keywords")
    keyword: "Keyword" = Relationship(back_populates="article_keywords")


# Resolve forward references
from app.models.keyword import Keyword  # noqa: E402, F811
from app.models.news import NewsArticle  # noqa: E402, F811

ArticleKeyword.model_rebuild()
