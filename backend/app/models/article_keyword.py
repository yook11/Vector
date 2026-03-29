from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.keyword import Keyword
    from app.models.news_article import NewsArticle


class ArticleKeyword(Base):
    __tablename__ = "article_keywords"

    news_article_id: Mapped[int] = mapped_column(
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    keyword_id: Mapped[int] = mapped_column(
        ForeignKey("keywords.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Relationships
    keyword: Mapped[Keyword] = relationship(back_populates="article_keywords")
    news_article: Mapped[NewsArticle] = relationship(back_populates="article_keywords")
