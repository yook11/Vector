from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime
from sqlmodel import Field, Relationship, SQLModel


class FetchStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class FetchLog(SQLModel, table=True):
    __tablename__ = "fetch_logs"

    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="news_sources.id", nullable=False, index=True)
    status: FetchStatus = Field(max_length=20, nullable=False)
    articles_count: int = Field(default=0, nullable=False)
    error_message: str | None = Field(default=None)
    duration_ms: int | None = Field(default=None)
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )

    # Relationships
    source: "NewsSource" = Relationship(back_populates="fetch_logs")


# Resolve forward references
from app.models.news_source import NewsSource  # noqa: E402, F811

FetchLog.model_rebuild()
