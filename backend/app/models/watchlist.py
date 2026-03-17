from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class WatchlistItem(SQLModel, table=True):
    __tablename__ = "watchlists"
    __table_args__ = (
        UniqueConstraint("user_id", "news_article_id", name="uq_user_watchlist"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(sa_column=Column(String(32), nullable=False, index=True))
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
    news_article: "NewsArticle" = Relationship(back_populates="watchlist_items")


# Resolve forward references
from app.models.news import NewsArticle  # noqa: E402

WatchlistItem.model_rebuild()
