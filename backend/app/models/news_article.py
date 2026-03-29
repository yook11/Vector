from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlmodel import Field, Relationship, SQLModel


class NewsArticle(SQLModel, table=True):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("original_url", name="uq_news_articles_original_url"),
        Index("idx_news_published", "published_at", postgresql_using="btree"),
        Index(
            "idx_content_fetch_pending",
            "skip_content_fetch",
            postgresql_where=text(
                "original_content IS NULL AND skip_content_fetch = false"
            ),
        ),
        Index(
            "idx_news_source_published",
            "news_source_id",
            text("published_at DESC"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)

    # --- Primary columns (new schema, used by application code) ---
    original_title: str = Field(max_length=500, nullable=False)
    original_url: str = Field(max_length=2048, nullable=False)
    original_content: str | None = Field(default=None, sa_type=Text())
    original_description: str | None = Field(default=None, max_length=2000)
    news_source_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey(
                "news_sources.id",
                ondelete="RESTRICT",
                name="fk_news_articles_news_source_id",
            ),
            nullable=False,
        )
    )
    published_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        ),
    )

    skip_content_fetch: bool = Field(default=False, nullable=False)

    # --- Relationships ---
    article_analysis: Optional["ArticleAnalysis"] = Relationship(
        back_populates="news_article",
        sa_relationship_kwargs={"uselist": False},
    )
    news_source: "NewsSource" = Relationship(
        back_populates="articles",
        sa_relationship_kwargs={
            "uselist": False,
            "foreign_keys": "[NewsArticle.news_source_id]",
        },
    )
    # article_keywords: cross-base (ArticleKeyword is DeclarativeBase) — FK only
    watchlist_entries: list["WatchlistEntry"] = Relationship(
        back_populates="news_article"
    )


# Resolve forward references
from app.models.article_analysis import ArticleAnalysis  # noqa: E402, F811
from app.models.news_source import NewsSource  # noqa: E402, F811
from app.models.watchlist_entry import WatchlistEntry  # noqa: E402, F811

NewsArticle.model_rebuild()
