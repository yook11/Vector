"""Pydantic schemas for news_sources CRUD endpoints (SSoT)."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.models.news_source import SourceType
from app.utils.sanitize import validate_url_scheme

# --- XSS protection: source name whitelist ---
# Allows Unicode word chars, spaces, hyphens, dots.
# Rejects HTML-significant characters (< > & " ').
# (?=.*\w) requires at least one word character.
_SOURCE_NAME_RE = re.compile(r"^(?=.*\w)[\w \-\.]+$", re.UNICODE)


class NewsSourceCreate(BaseModel):
    """POST /api/v1/sources request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    name: str = Field(min_length=1, max_length=50)
    source_type: SourceType
    site_url: str = Field(max_length=2048)
    endpoint_url: str = Field(max_length=2048)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("name", mode="after")
    @classmethod
    def validate_name_chars(cls, v: str) -> str:
        if not _SOURCE_NAME_RE.match(v):
            raise ValueError(
                "Source name can only contain letters, numbers, spaces, "
                "hyphens, dots, and underscores"
            )
        return v

    @field_validator("site_url")
    @classmethod
    def validate_site_url(cls, v: str) -> str:
        return validate_url_scheme(v, "site_url")

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, v: str) -> str:
        return validate_url_scheme(v, "endpoint_url")


class NewsSourceUpdate(BaseModel):
    """PUT /api/v1/sources/{id} request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    name: str | None = Field(default=None, min_length=1, max_length=50)
    source_type: SourceType | None = None
    site_url: str | None = Field(default=None, max_length=2048)
    endpoint_url: str | None = Field(default=None, max_length=2048)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("name", mode="after")
    @classmethod
    def validate_name_chars(cls, v: str | None) -> str | None:
        if v is not None and not _SOURCE_NAME_RE.match(v):
            raise ValueError(
                "Source name can only contain letters, numbers, spaces, "
                "hyphens, dots, and underscores"
            )
        return v

    @field_validator("site_url")
    @classmethod
    def validate_site_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_url_scheme(v, "site_url")
        return None

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_url_scheme(v, "endpoint_url")
        return None


class NewsSourceResponse(BaseModel):
    """Single news source in API responses."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    name: str
    source_type: SourceType
    site_url: str
    endpoint_url: str
    is_active: bool
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
