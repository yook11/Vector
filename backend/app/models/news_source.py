from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime
from sqlmodel import Field, Relationship, SQLModel


class SourceType(StrEnum):
    RSS = "rss"
    API = "api"


class NewsSource(SQLModel, table=True):
    __tablename__ = "news_sources"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=200, nullable=False)
    source_type: str = Field(max_length=20, nullable=False)
    site_url: str | None = Field(default=None, max_length=2048)
    is_active: bool = Field(default=True, nullable=False)
    fetch_interval_minutes: int = Field(default=720, nullable=False)
    next_fetch_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    last_fetched_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    consecutive_errors: int = Field(default=0, nullable=False)
    last_error_message: str | None = Field(default=None)

    # RSS-specific (NULL for API sources)
    feed_url: str | None = Field(default=None, max_length=2048, unique=True)
    etag: str | None = Field(default=None, max_length=256)
    last_modified_header: str | None = Field(default=None, max_length=256)

    # API-specific (NULL for RSS sources)
    api_endpoint: str | None = Field(default=None, max_length=200)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    articles: list["NewsArticle"] = Relationship(back_populates="source_ref")


# Resolve forward references
from app.models.news import NewsArticle  # noqa: E402, F811

NewsSource.model_rebuild()
