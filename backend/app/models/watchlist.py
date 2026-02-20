from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class WatchlistItem(SQLModel, table=True):
    __tablename__ = "watchlists"
    __table_args__ = (
        UniqueConstraint("user_id", "news_article_id", name="uq_user_watchlist"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    news_article_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("news_articles.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    user: "User" = Relationship(back_populates="watchlist_items")
    news_article: "NewsArticle" = Relationship(back_populates="watchlist_items")


# Resolve forward references
from app.models.news import NewsArticle  # noqa: E402, F811
from app.models.user import User  # noqa: E402, F811

WatchlistItem.model_rebuild()
