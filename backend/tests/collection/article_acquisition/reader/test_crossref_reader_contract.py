"""Crossref Reader の契約テスト。

公開メソッド ``CrossrefReader.fetch_works`` に録画 transport を渡し、Reader が
item を drop しないこと (R3) と、payload 全体の失敗だけを typed error に写す
こと (R4) を確認する。収集スコープ判定は Source 側の責務。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.article_acquisition.reader.crossref_reader import (
    CrossrefEntry,
    CrossrefReader,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures (C1 と同一)
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"
_MOD = "app.collection.article_acquisition.reader.crossref_reader"
_TOOLS_MOD = "app.collection.article_acquisition.tools.reader_tools"
_FIXTURE = "mdpi_crossref.json"
_CONTACT_EMAIL = "crossref-contact@example.invalid"


def _raw_items() -> list[dict[str, Any]]:
    """録画 payload の item 列 (count parity の期待値を標本から導出)。"""
    raw = json.loads((_FIXTURES_DIR / _FIXTURE).read_text())
    return list(raw["message"]["items"])


def _response(status_code: int, content: bytes) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=content,
        request=httpx.Request("GET", "https://api.crossref.org/works"),
    )


async def _reader_entries() -> list[CrossrefEntry]:
    """本物の ``CrossrefReader.fetch_works`` を録画実バイトで走らせる。

    差し替えるのは HTTP transport のみ。json decode / item→Entry 抽出は
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
        return await CrossrefReader(contact_email=_CONTACT_EMAIL).fetch_works(
            source_name="crossref-reader-contract",
            issn="0000-0000",
            from_pub_date="2000-01-01",
            rows=100,
        )


async def test_reader_drops_no_recorded_item() -> None:
    """R3 真の no-drop: Reader 出力件数は録画 item 件数と 1:1。

    収集スコープ判定 (type≠journal-article / 非 CC-BY / 短 abstract / date
    欠落) が Reader へ漏れれば件数が減る。期待件数は標本から導出し、
    ``correction`` entry が含まれることも確認する。
    """
    entries = await _reader_entries()
    assert len(entries) == len(_raw_items())  # 真の no-drop (件数 1:1)
    assert any(e.entry_type == "correction" for e in entries), [
        e.entry_type for e in entries
    ]  # 収集スコープ外 entry を標本が含むことの確認


async def _raise_through(status_code: int) -> None:
    response = _response(status_code, b'{"message":{"items":[]}}')

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        await CrossrefReader(contact_email=_CONTACT_EMAIL).fetch_works(
            source_name="crossref-reader-contract",
            issn="0000-0000",
            from_pub_date="2000-01-01",
            rows=100,
        )


async def test_http_403_raises_access_denied() -> None:
    """R4: payload 全体の失敗 (403) は ``ExternalFetchError`` 系に写る。"""
    with pytest.raises(FetchAccessDeniedError):
        await _raise_through(403)


async def test_http_500_raises_origin_server_error() -> None:
    """R4: payload 全体の失敗 (500) は ``ExternalFetchError`` 系に写る。"""
    with pytest.raises(FetchOriginServerError):
        await _raise_through(500)


async def _fetch_body(content: bytes) -> list[CrossrefEntry]:
    """200 応答に任意 body を載せて本物の ``fetch_works`` を走らせる。"""
    response = _response(200, content)

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        return await CrossrefReader(contact_email=_CONTACT_EMAIL).fetch_works(
            source_name="crossref-reader-contract",
            issn="0000-0000",
            from_pub_date="2000-01-01",
            rows=100,
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
            b'{"message": []}',
            UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
            "message",
            id="message_not_dict",
        ),
        pytest.param(
            b'{"message": {"items": {}}}',
            UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
            "items",
            id="items_not_list",
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


async def test_empty_items_is_success_not_unreadable() -> None:
    """正常な空 items は成功 (空列) で、unreadable に倒さない。"""
    assert await _fetch_body(b'{"message":{"items":[]}}') == []


async def test_reader_tools_injects_contact_without_real_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定層の連絡先をReaderへ注入し、HTTP境界はmock内に閉じる。"""
    captured_headers: dict[str, str] = {}
    response = _response(200, b'{"message":{"items":[]}}')

    monkeypatch.setattr(
        f"{_TOOLS_MOD}.settings",
        SimpleNamespace(crossref_contact_email=_CONTACT_EMAIL),
    )

    @asynccontextmanager
    async def _fake_safe_client(**kwargs: Any) -> AsyncIterator[Any]:
        captured_headers.update(kwargs["headers"])
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        await ReaderTools().crossref.fetch_works(
            source_name="crossref-reader-contract",
            issn="0000-0000",
            from_pub_date="2000-01-01",
            rows=1,
        )

    assert f"mailto:{_CONTACT_EMAIL}" in captured_headers["User-Agent"]
