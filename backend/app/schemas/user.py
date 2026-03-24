from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class WatchlistCreate(BaseModel):
    """POST /api/v1/me/watchlist request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    news_article_id: int


class WatchlistResponse(BaseModel):
    """Watchlist item in API responses."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    news_article_id: int
    title_original: str
    url: str
    source: str
    published_at: datetime | None = None
    created_at: datetime


class WatchlistListResponse(BaseModel):
    """GET /api/v1/me/watchlist response wrapper."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[WatchlistResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
