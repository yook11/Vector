import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlmodel import Field, Relationship, SQLModel


class WatchlistEntry(SQLModel, table=True):
    __tablename__ = "watchlist_entries"

    user_id: uuid_mod.UUID = Field(
        sa_column=Column(
            PgUUID(as_uuid=True),
            ForeignKey("auth.user.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    news_article_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("news_articles.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        )
    )

    # Relationships
    news_article: "NewsArticle" = Relationship(back_populates="watchlist_entries")


# Resolve forward references
from app.models.news_article import NewsArticle  # noqa: E402

WatchlistEntry.model_rebuild()
