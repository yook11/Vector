from typing import Annotated

from fastapi import Query
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict
from pydantic.dataclasses import dataclass

from app.schemas.articles import ARTICLE_LIST_LIMIT, ArticleId
from app.schemas.base import _CamelBase
from app.schemas.cursor import CURSOR_JSON_SCHEMA, CursorPosition


class WatchlistIdsParams(BaseModel):
    """GET /api/v1/me/watchlist/ids のクエリパラメータ。

    問い合わせるのは記事一覧の1回の取得分なので、その上限 (ARTICLE_LIST_LIMIT) を
    超える数の記事 ID は受け付けない。
    """

    article_ids: Annotated[
        list[ArticleId],
        Query(alias="articleIds", min_length=1, max_length=ARTICLE_LIST_LIMIT),
    ]


class WatchlistIds(_CamelBase):
    """GET /api/v1/me/watchlist/ids のレスポンス。

    渡した記事のうちウォッチ中のものの ID。記事のレスポンスはユーザーに依存させず、
    frontend が表示時にこの集合と突き合わせる。
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
