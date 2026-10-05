"""一覧カーソルの文字列表現の試験。"""

import base64
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas.articles import ArticleListParams, ArticleListPosition
from app.schemas.cursor import encode_cursor
from app.schemas.watchlist import WatchlistPosition


def _cursor_from_json(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def test_encoded_position_reads_back_with_microseconds_and_timezone() -> None:
    position = ArticleListPosition(
        published_at=datetime(
            2026, 10, 5, 10, 2, 3, 123456, tzinfo=timezone(timedelta(hours=9))
        ),
        id=42,
    )

    params = ArticleListParams(cursor=encode_cursor(position))

    assert params.cursor == position
    assert params.cursor.published_at == datetime(
        2026, 10, 5, 1, 2, 3, 123456, tzinfo=UTC
    )


def test_cursor_is_url_safe_without_padding() -> None:
    position = ArticleListPosition(
        published_at=datetime(2026, 10, 5, tzinfo=UTC), id=2_147_483_647
    )

    cursor = encode_cursor(position)

    assert cursor.replace("-", "").replace("_", "").isalnum()


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("not base64!", id="base64 の文字以外を含む"),
        pytest.param(_cursor_from_json("not json"), id="JSON でない"),
        pytest.param(
            _cursor_from_json('{"published_at": "2026-10-05T01:02:03Z"}'),
            id="id が無い",
        ),
        pytest.param(
            _cursor_from_json(
                '{"published_at": "2026-10-05T01:02:03Z", "id": 1, "extra": 1}'
            ),
            id="余計な項目がある",
        ),
        pytest.param(
            _cursor_from_json('{"published_at": "2026-10-05T01:02:03", "id": 1}'),
            id="時刻にタイムゾーンが無い",
        ),
        pytest.param(
            _cursor_from_json(
                '{"published_at": "2026-10-05T01:02:03Z", "id": 2147483648}'
            ),
            id="id が記事 ID の上限を超える",
        ),
        pytest.param(
            encode_cursor(
                WatchlistPosition(
                    watched_at=datetime(2026, 10, 5, tzinfo=UTC), article_id=1
                )
            ),
            id="ウォッチリストのカーソル",
        ),
        pytest.param("A" * 257, id="257 字"),
    ],
)
def test_unreadable_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(ValidationError):
        ArticleListParams(cursor=cursor)
