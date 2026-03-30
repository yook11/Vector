from datetime import datetime

from app.schemas.base import _CamelBase


class WatchlistCreate(_CamelBase):
    """POST /api/v1/me/watchlist request body."""

    news_article_id: int


class WatchlistResponse(_CamelBase):
    """Watchlist item in API responses."""

    news_article_id: int
    original_title: str
    original_url: str
    source_name: str
    published_at: datetime | None = None
    created_at: datetime


class WatchlistListResponse(_CamelBase):
    """GET /api/v1/me/watchlist response wrapper."""

    items: list[WatchlistResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
