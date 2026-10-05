from typing import Annotated

from fastapi import Query
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict
from pydantic.dataclasses import dataclass

from app.schemas.articles import ArticleId
from app.schemas.base import _CamelBase
from app.schemas.cursor import CURSOR_JSON_SCHEMA, CursorPosition


class WatchlistIds(_CamelBase):
    """GET /api/v1/me/watchlist/ids のレスポンス。

    記事リソースから per-user フラグを切り離し、frontend が render 時に
    Set lookup で merge するための per-user メンバーシップ ID 集合。
    """

    ids: list[int]


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class WatchlistPosition(CursorPosition):
    """ウォッチリストの並び上の位置。

    並びはウォッチした時刻の新しい順で、同時刻は記事 ID の大きい順。
    """

    watched_at: AwareDatetime
    article_id: ArticleId


WatchlistCursor = Annotated[
    WatchlistPosition,
    BeforeValidator(WatchlistPosition.from_cursor),
    CURSOR_JSON_SCHEMA,
]


class WatchlistParams(BaseModel):
    """ウォッチリスト一覧のクエリパラメータ。カーソルの形式が不正なら 422 を返す。"""

    cursor: Annotated[WatchlistCursor | None, Query()] = None
