from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Column, DateTime, String, UniqueConstraint, func
from sqlmodel import Field, Relationship, SQLModel


class SourceType(StrEnum):
    RSS = "rss"
    API = "api"


class NewsSource(SQLModel, table=True):
    __tablename__ = "news_sources"
    __table_args__ = (
        UniqueConstraint(
            "name", "source_type", name="uq_news_sources_name_source_type"
        ),
        CheckConstraint(
            "source_type IN ('rss', 'api')",
            name="ck_news_sources_source_type",
        ),
        CheckConstraint(
            "site_url ~ '^https?://.+'",
            name="ck_news_sources_site_url_scheme",
        ),
        CheckConstraint(
            "endpoint_url ~ '^https?://.+'",
            name="ck_news_sources_endpoint_url_scheme",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=50, nullable=False)
    source_type: SourceType = Field(sa_type=String(20), nullable=False)
    site_url: str = Field(max_length=2048, nullable=False)
    endpoint_url: str = Field(max_length=2048, unique=True, nullable=False)
    is_active: bool = Field(default=True, nullable=False)

    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        ),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        ),
    )

    # Relationships
    articles: list["NewsArticle"] = Relationship(
        back_populates="news_source",
        sa_relationship_kwargs={
            "foreign_keys": "[NewsArticle.news_source_id]",
        },
    )
    fetch_logs: list["FetchLog"] = Relationship(back_populates="source")


# Resolve forward references
from app.models.fetch_log import FetchLog  # noqa: E402, F811
from app.models.news import NewsArticle  # noqa: E402, F811

NewsSource.model_rebuild()
