from app.schemas.base import _CamelBase


class WatchlistIds(_CamelBase):
    """GET /api/v1/me/watchlist/ids のレスポンス。

    記事リソースから per-user フラグを切り離し、frontend が render 時に
    Set lookup で merge するための per-user メンバーシップ ID 集合。
    """

    ids: list[int]
