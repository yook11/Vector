"""Hacker News Reader の契約テスト。

公開メソッド ``HackerNewsReader.search_recent_stories`` に録画 transport を
渡し、Reader が hit を drop しないこと (R3) と、payload 全体の失敗だけを
typed error に写すこと (R4) を確認する。個別 hit の要否判定は後段の責務。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.article_acquisition.reader.algolia_hn_reader import (
    HackerNewsEntry,
    HackerNewsReader,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures (C1 と同一)
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"
_MOD = "app.collection.article_acquisition.reader.algolia_hn_reader"
_FIXTURE = "hacker_news_hits.json"


def _raw_hits() -> list[dict[str, Any]]:
    """録画 payload の hit 列 (count parity の期待値を標本から導出)。"""
    raw = json.loads((_FIXTURES_DIR / _FIXTURE).read_text())
    return list(raw["hits"])


def _response(status_code: int, content: bytes) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=content,
        request=httpx.Request("GET", "https://hn.algolia.com/api/v1/search_by_date"),
    )


async def _reader_entries() -> list[HackerNewsEntry]:
    """本物の ``HackerNewsReader.search_recent_stories`` を録画実バイトで走らせる。

    差し替えるのは HTTP transport のみ。json decode / hit→Entry 抽出は
    Reader 内部で本物が動く。
    """
    raw = (_FIXTURES_DIR / _FIXTURE).read_bytes()
    response = _response(200, raw)

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        return await HackerNewsReader().search_recent_stories(
            source_name="hn-reader-contract",
            min_points=0,
            window_seconds=10**12,
            hits_per_page=100,
        )


async def test_reader_drops_no_recorded_hit() -> None:
    """R3 真の no-drop: Reader 出力件数は録画 hit 件数と 1:1。

    url 無しなどの hit 判定が Reader へ漏れれば件数が減る。期待件数は標本から
    導出し、``url=None`` の hit が含まれることも確認する。
    """
    entries = await _reader_entries()
    assert len(entries) == len(_raw_hits())  # 真の no-drop (件数 1:1)
    assert any(e.url is None for e in entries), [
        e.url for e in entries
    ]  # url 無し hit を標本が含むことの確認


async def _raise_through(status_code: int) -> None:
    response = _response(status_code, b"{}")

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        await HackerNewsReader().search_recent_stories(
            source_name="hn-reader-contract",
            min_points=0,
            window_seconds=10**12,
            hits_per_page=100,
        )


async def test_http_403_raises_access_denied() -> None:
    """R4: payload 全体の失敗 (403) は ``ExternalFetchError`` 系に写る。"""
    with pytest.raises(FetchAccessDeniedError):
        await _raise_through(403)


async def test_http_500_raises_origin_server_error() -> None:
    """R4: payload 全体の失敗 (500) は ``ExternalFetchError`` 系に写る。"""
    with pytest.raises(FetchOriginServerError):
        await _raise_through(500)


async def _fetch_body(content: bytes) -> list[HackerNewsEntry]:
    """200 応答に任意 body を載せて本物の ``search_recent_stories`` を走らせる。"""
    response = _response(200, content)

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        return await HackerNewsReader().search_recent_stories(
            source_name="hn-reader-contract",
            min_points=0,
            window_seconds=10**12,
            hits_per_page=100,
        )


@pytest.mark.parametrize(
    "body,expected_reason,expected_field",
    [
        pytest.param(b"", UnreadableResponseReason.EMPTY_BODY, None, id="empty_body"),
        pytest.param(
            b"   ", UnreadableResponseReason.EMPTY_BODY, None, id="whitespace_body"
        ),
        pytest.param(
            b"{not valid json",
            UnreadableResponseReason.MALFORMED_CONTENT,
            None,
            id="json_decode_error",
        ),
        pytest.param(
            b"[]",
            UnreadableResponseReason.UNEXPECTED_ROOT_SHAPE,
            "data",
            id="top_level_not_dict",
        ),
        pytest.param(
            b'{"hits": {}}',
            UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
            "hits",
            id="hits_not_list",
        ),
    ],
)
async def test_unreadable_payload_classified_by_reason(
    body: bytes,
    expected_reason: UnreadableResponseReason,
    expected_field: str | None,
) -> None:
    """接続成功だが構造化不能な payload は read 段固有の ``UnreadableResponseError``
    に写り、**どこがどう壊れたか** を reason + field で自己記述する (接続境界
    ``ExternalFetchError`` とは別系統。生 ``JSONDecodeError`` / ``AttributeError`` を
    上位へ漏らさない)。
    """
    with pytest.raises(UnreadableResponseError) as raised:
        await _fetch_body(body)

    exc = raised.value
    assert exc.reason is expected_reason
    assert exc.field == expected_field
    assert exc.response_format == "json"


async def test_empty_hits_is_success_not_unreadable() -> None:
    """正常な空 hits は成功 (空列) で、unreadable に倒さない。"""
    assert await _fetch_body(b'{"hits":[]}') == []
