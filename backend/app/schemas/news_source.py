"""Pydantic schemas for news_sources CRUD endpoints (SSoT)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel


class NewsSourceCreate(BaseModel):
    """POST /api/v1/sources request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    name: str
    source_type: str  # "rss" | "api"
    site_url: str | None = None
    feed_url: str | None = None
    api_endpoint: str | None = None
    fetch_interval_minutes: int = 720

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, v: str) -> str:
        if v not in ("rss", "api"):
            raise ValueError("source_type must be 'rss' or 'api'")
        return v

    @field_validator("fetch_interval_minutes")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if not (15 <= v <= 1440):
            raise ValueError("fetch_interval_minutes must be between 15 and 1440")
        return v


class NewsSourceUpdate(BaseModel):
    """PUT /api/v1/sources/{id} request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    name: str | None = None
    source_type: str | None = None
    site_url: str | None = None
    feed_url: str | None = None
    api_endpoint: str | None = None
    fetch_interval_minutes: int | None = None

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, v: str | None) -> str | None:
        if v is not None and v not in ("rss", "api"):
            raise ValueError("source_type must be 'rss' or 'api'")
        return v

    @field_validator("fetch_interval_minutes")
    @classmethod
    def validate_interval(cls, v: int | None) -> int | None:
        if v is not None and not (15 <= v <= 1440):
            raise ValueError("fetch_interval_minutes must be between 15 and 1440")
        return v


class NewsSourceResponse(BaseModel):
    """Single news source in API responses."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    name: str
    source_type: str
    site_url: str | None = None
    is_active: bool
    feed_url: str | None = None
    api_endpoint: str | None = None
    fetch_interval_minutes: int
    next_fetch_at: datetime | None = None
    last_fetched_at: datetime | None = None
    consecutive_errors: int = 0
    last_error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class NewsSourceListResponse(BaseModel):
    """GET /api/v1/sources response wrapper."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[NewsSourceResponse]
    total: int
