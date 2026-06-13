from typing import Annotated

from pydantic import Field

from app.schemas.base import _CamelBase

# 公開 article_id は PostgreSQL INTEGER (int32)。範囲外値で asyncpg が
# OverflowError を上げて 500 leak になるのを構造的に閉塞するため、
# request body の field level で上下限を明示する。
_INT32_MAX = 2_147_483_647


class WatchlistCreate(_CamelBase):
    """POST /api/v1/me/watchlist のリクエストボディ。"""

    article_id: Annotated[int, Field(ge=1, le=_INT32_MAX)]


class WatchlistIds(_CamelBase):
    """GET /api/v1/me/watchlist/ids のレスポンス。

    記事リソースから per-user フラグを切り離し、frontend が render 時に
    Set lookup で merge するための per-user メンバーシップ ID 集合。
    """

    ids: list[int]
