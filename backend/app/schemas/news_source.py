"""Pydantic schemas for news_sources CRUD endpoints (SSoT)."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.utils.sanitize import validate_url_scheme

# --- XSS対策: ソース名のホワイトリスト ---
# display_name と同じ方針。HTMLタグに使われる < > & " ' 等を排除する。
#
# \w (re.UNICODE): Unicode文字 + 英数字 + アンダースコア
# リテラルスペース: \s ではなく " " に限定（タブ・改行・ゼロ幅スペースを排除）
# ハイフン、ドット: ソース名に含まれる（例: "Bloomberg L.P.", "Alpha Vantage - Tech"）
# (?=.*\w): 少なくとも1文字の\wを要求（"..." や "   " だけの文字列を弾く）
_SOURCE_NAME_RE = re.compile(r"^(?=.*\w)[\w \-\.]+$", re.UNICODE)


class NewsSourceCreate(BaseModel):
    """POST /api/v1/sources request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    name: str = Field(min_length=1, max_length=200)
    source_type: str  # "rss" | "api"
    site_url: str | None = None
    feed_url: str | None = None
    api_endpoint: str | None = None
    fetch_interval_minutes: int = 720

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: object) -> object:
        """Strip whitespace before length validation.

        mode="before" receives raw input (any type), so we guard with isinstance.
        """
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("name", mode="after")
    @classmethod
    def validate_name_chars(cls, v: str) -> str:
        """Whitelist validation: reject characters not in the allowed set."""
        if not _SOURCE_NAME_RE.match(v):
            raise ValueError(
                "Source name can only contain letters, numbers, spaces, "
                "hyphens, dots, and underscores"
            )
        return v

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, v: str) -> str:
        if v not in ("rss", "api"):
            raise ValueError("source_type must be 'rss' or 'api'")
        return v

    # --- XSS対策: URLスキームのホワイトリスト ---
    # 管理者が登録する feed_url / site_url に対しても、
    # http/https 以外のスキーム（javascript: 等）を拒否する。
    # 管理者アカウント乗っ取り時の被害を限定するための多層防御。
    @field_validator("site_url")
    @classmethod
    def validate_site_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_url_scheme(v, "site_url")
        return None

    @field_validator("feed_url")
    @classmethod
    def validate_feed_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_url_scheme(v, "feed_url")
        return None

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

    name: str | None = Field(default=None, min_length=1, max_length=200)
    source_type: str | None = None
    site_url: str | None = None
    feed_url: str | None = None
    api_endpoint: str | None = None
    fetch_interval_minutes: int | None = None

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: object) -> object:
        """Strip whitespace before length validation.

        mode="before" receives raw input (any type), so we guard with isinstance.
        """
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

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, v: str | None) -> str | None:
        if v is not None and v not in ("rss", "api"):
            raise ValueError("source_type must be 'rss' or 'api'")
        return v

    @field_validator("site_url")
    @classmethod
    def validate_site_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_url_scheme(v, "site_url")
        return None

    @field_validator("feed_url")
    @classmethod
    def validate_feed_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_url_scheme(v, "feed_url")
        return None

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
