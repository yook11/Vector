from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlmodel import Field, Relationship, SQLModel


class FetchStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class FetchLog(SQLModel, table=True):
    __tablename__ = "fetch_logs"

    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey(
                "news_sources.id",
                ondelete="CASCADE",
                name="fk_fetch_logs_source_id",
            ),
            nullable=False,
            index=True,
        )
    )
    status: FetchStatus = Field(sa_type=String(20), nullable=False)
    articles_count: int = Field(default=0, nullable=False)
    error_message: str | None = Field(default=None, sa_type=Text())
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
