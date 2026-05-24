"""Hacker News Reader の契約テスト (凍らせた実標本 × 性質)。

C1 [test_rss_reader_contract.py](./test_rss_reader_contract.py) の HN 姉妹。
普遍オラクル [test_reader_role_contract.py](./test_reader_role_contract.py) が
見るのは R1/R5 (typed Entry 箱の*形*) だけ。本テストはその射程外の
**R3 (per-item を drop / 裁かない) と R4 (typed-error 境界 = transport/payload
全体のみ)** を HN Reader について固定する (計画「三層目 / 用法(i)」)。

なぜ要るか: Step 1 は hit→値抽出を ``HackerNewsSource`` から HN Reader へ
*移動*する操作。最も起きやすい退行は「移行元の per-item drop (url=None /
空 title skip) が抽出と一緒に Reader へ流れ込む」こと。これが起きても普遍
オラクルは緑のまま (typed 箱の形しか見ない) = 偽 all-clear。本テストが
その退行を**発見**する唯一の oracle。

契約は **Reader の公開メソッド ``HackerNewsReader.search_recent_stories``
を通して**確かめる。差し替えるのは HTTP transport (``make_safe_async_client``)
**のみ**で、json decode / hit→Entry 抽出は Reader 内部で本物が動く。本テストが
知るのは公開 entrypoint と ``HackerNewsEntry`` だけで、``normalize_hit`` 等の
Reader 内臓は一切 import しない (C1 が ``normalize_entry`` を import しないのと
同形)。

見る性質:

- **R3 no-drop = count parity**: Reader 出力の件数は録画 payload の hit 件数
  と 1:1。これが真の no-drop 不変条件 (per-item drop が抽出と一緒に Reader
  へ部分的にでも漏れれば件数が減る = 検出)。``any(url is None)`` だけの
  witness では部分漏れを緑で見逃すため不十分。期待件数は標本から導出し
  literal を直書きしない (録り直しに自己追従 = litmus 適合)。``url=None``
  の Ask HN / Show HN 系 hit が出力に現れることを「標本が degenerate な形を
  実際に踏む」provenance の非空虚証明として併置する (要否判定は後段
  converter の責務)。Algolia HN feed は常に Ask HN テキスト投稿 (url=None)
  を含むため非空虚 (Ask HN 投稿が feed から消える日が来たら R3 標本を
  選び直す = provenance 規律であって CI flake ではない)。
- **R4 typed-error 境界**: HTTP status / transport 例外 = payload **全体**の
  失敗のみ ``ExternalFetchError`` 系に写る。個別 hit の値不良では raise
  しない (= R3 の no-drop で表現される。値不良 → ``ConversionRejection`` は
  後段 converter/fetcher 層が既所有)。

**赤の triage (厳守)**: red は ``algolia_hn_reader.py`` の修正 (実挙動が真の
契約に反する = 抽出移動で drop が Reader へ漏れた等) か、assert の over-claim
認定のどちらかで解消する。**現コードの偶発出力に合わせて assert を緩める
ことは禁止** (C1 と同 doctrine)。

litmus: 標本を録り直して赤くなるなら中身を見ていた = 間違い。count parity は
標本由来件数との比較なので録り直しに自己追従し、HN Reader が永遠に守る性質
しか見ないため feed の文面が変わっても赤くならない。
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

from app.collection.article_collection.errors import UnreadableResponseError
from app.collection.article_collection.reader.algolia_hn_reader import (
    HackerNewsEntry,
    HackerNewsReader,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures (C1 と同一)
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"
_MOD = "app.collection.article_collection.reader.algolia_hn_reader"
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

    旧 ``HackerNewsSource.collect`` は url 無し / 空 title hit を ``continue``
    で silent drop していた。抽出を Reader へ移すとき、その判定が一緒に
    Reader へ漏れれば件数が減る = 検出。期待件数は標本から導出 (literal 直書き
    しない = 録り直し自己追従)。``url=None`` witness は標本が degenerate な形を
    実際に踏む非空虚証明として併置 (判定は後段 converter の責務)。
    """
    entries = await _reader_entries()
    assert len(entries) == len(_raw_hits())  # 真の no-drop (件数 1:1)
    assert any(e.url is None for e in entries), [
        e.url for e in entries
    ]  # 非空虚 witness (degenerate な形を標本が踏む provenance 証明)


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
    "body",
    [
        pytest.param(b"{not valid json", id="json_decode_error"),
        pytest.param(b"[]", id="top_level_not_dict"),
        pytest.param(b'{"hits": {}}', id="hits_not_list"),
    ],
)
async def test_unreadable_payload_raises_unreadable_response(body: bytes) -> None:
    """接続は成功したが構造化できない payload (JSON decode 失敗 / envelope shape
    不正) は read 段固有の ``UnreadableResponseError`` に写る (接続境界
    ``ExternalFetchError`` とは別系統)。生 ``JSONDecodeError`` / ``AttributeError``
    を上位に漏らさない。"""
    with pytest.raises(UnreadableResponseError):
        await _fetch_body(body)


async def test_empty_hits_is_success_not_unreadable() -> None:
    """正常な空 hits は成功 (空列) で、unreadable に倒さない。"""
    assert await _fetch_body(b'{"hits":[]}') == []
