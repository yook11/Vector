"""Pydantic schemas for news_sources CRUD endpoints (SSoT)."""

from datetime import datetime

from app.domain.news_source import SourceName
from app.domain.safe_url import SafeUrl
from app.models.news_source import SourceType
from app.schemas.base import _CamelBase


class NewsSourceCreate(_CamelBase):
    """POST /api/v1/sources request body."""

    name: SourceName
    source_type: SourceType
    site_url: SafeUrl
    endpoint_url: SafeUrl


class NewsSourceResponse(_CamelBase):
    """Single news source in API responses."""

    id: int
    name: SourceName
    source_type: SourceType
    site_url: SafeUrl
    endpoint_url: SafeUrl
    is_active: bool
    created_at: datetime
    updated_at: datetime


class NewsSourceListResponse(_CamelBase):
    """GET /api/v1/sources response wrapper."""

    items: list[NewsSourceResponse]
    total: int
