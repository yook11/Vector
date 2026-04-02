from app.schemas.base import _CamelBase


class WatchlistCreate(_CamelBase):
    """POST /api/v1/me/watchlist request body."""

    news_id: int
