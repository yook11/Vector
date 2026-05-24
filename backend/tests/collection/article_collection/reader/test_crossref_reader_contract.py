"""Crossref Reader の契約テスト (凍らせた実標本 × 性質)。

C1 [test_rss_reader_contract.py](./test_rss_reader_contract.py) の Crossref
姉妹。普遍オラクル [test_reader_role_contract.py](./test_reader_role_contract.py)
が見るのは R1/R5 (typed Entry 箱の*形*) だけ。本テストはその射程外の
**R3 (per-item を drop / 裁かない) と R4 (typed-error 境界 = transport/payload
全体のみ)** を Crossref Reader について固定する (計画「三層目 / 用法(i)」)。

なぜ要るか: Step 2 は item→値抽出を ``mdpi/_common.py`` から Crossref Reader
へ*移動*する操作。最も起きやすい退行は「移行元の per-item 除外 (type≠
journal-article / 非 CC-BY / 短 abstract / date 欠落 = ソースの**収集スコープ
判定**) が抽出と一緒に Reader へ流れ込む」こと。スコープ判定は Source の
責務であって Reader のものではない (対象外 ≠ 変換失敗 ≠ 構造的非記事)。
これが起きても普遍オラクルは緑のまま (typed 箱の形しか見ない) = 偽
all-clear。本テストがその退行を**発見**する唯一の oracle。

契約は **Reader の公開メソッド ``CrossrefReader.fetch_works`` を通して**確かめる。
差し替えるのは HTTP transport (``make_safe_async_client``) **のみ**で、json
decode / item→Entry 抽出 (JATS strip / date parse / DOI 抽出) は Reader 内部で
本物が動く。本テストが知るのは公開 entrypoint と ``CrossrefEntry`` だけで、
``normalize_item`` 等の Reader 内臓は一切 import しない (C1 が
``normalize_entry`` を import しないのと同形)。

見る性質:

- **R3 no-drop = count parity**: Reader 出力の件数は録画 payload の item 件数
  と 1:1。これが真の no-drop 不変条件 (収集スコープ判定が Reader へ部分的に
  でも漏れれば件数が減る = 検出)。``any(correction)`` だけの witness では
  部分漏れ (短 abstract / date 欠落のみ drop し correction は残す等) を緑で
  見逃すため不十分。期待件数は標本から導出し literal を直書きしない
  (録り直しに自己追従 = litmus 適合)。``type == "correction"`` witness は
  「標本が収集スコープ外の形を実際に踏む」provenance の非空虚証明として
  併置する (旧 ``mdpi_items`` は ``type != "journal-article"`` を ``continue``
  で silent-drop していた。スコープ判定は後段 Source 純粋述語
  ``is_collectable_mdpi_work`` の責務であって、対象外データは変換失敗でも
  構造的非記事でもない)。録画標本 ``mdpi_crossref.json`` は journal-article
  2 / correction 1 を含むため非空虚 (provenance 規律。Crossref が correction
  を返さなくなる日が来たら R3 標本を選び直す = CI flake ではない)。
- **R4 typed-error 境界**: HTTP status / transport 例外 = payload **全体**の
  失敗のみ ``ExternalFetchError`` 系に写る。個別 item の値不良では raise
  しない (= R3 の no-drop で表現される。値不良 → ``ConversionRejection`` は
  後段 converter/fetcher 層が既所有)。

**赤の triage (厳守)**: red は ``crossref_reader.py`` の修正 (実挙動が真の
契約に反する = 抽出移動でスコープ判定が Reader へ漏れた等) か、assert の
over-claim 認定のどちらかで解消する。**現コードの偶発出力に合わせて assert
を緩めることは禁止** (C1 と同 doctrine)。

litmus: 標本を録り直して赤くなるなら中身を見ていた = 間違い。count parity は
標本由来件数との比較なので録り直しに自己追従し、Crossref Reader が永遠に守る
性質しか見ないため feed の文面が変わっても赤くならない。
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
from app.collection.article_collection.reader.crossref_reader import (
    CrossrefEntry,
    CrossrefReader,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures (C1 と同一)
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"
_MOD = "app.collection.article_collection.reader.crossref_reader"
_FIXTURE = "mdpi_crossref.json"


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
        return await CrossrefReader().fetch_works(
            source_name="crossref-reader-contract",
            issn="0000-0000",
            from_pub_date="2000-01-01",
            rows=100,
        )


async def test_reader_drops_no_recorded_item() -> None:
    """R3 真の no-drop: Reader 出力件数は録画 item 件数と 1:1。

    収集スコープ判定 (type≠journal-article / 非 CC-BY / 短 abstract / date
    欠落) が抽出と一緒に Reader へ漏れれば件数が減る = 検出。期待件数は
    標本から導出 (literal 直書きしない = 録り直し自己追従)。``correction``
    witness は標本が収集スコープ外の形を実際に踏む非空虚証明として併置。
    """
    entries = await _reader_entries()
    assert len(entries) == len(_raw_items())  # 真の no-drop (件数 1:1)
    assert any(e.entry_type == "correction" for e in entries), [
        e.entry_type for e in entries
    ]  # 非空虚 witness (収集スコープ外の形を標本が踏む provenance 証明)


async def _raise_through(status_code: int) -> None:
    response = _response(status_code, b'{"message":{"items":[]}}')

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        await CrossrefReader().fetch_works(
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
        return await CrossrefReader().fetch_works(
            source_name="crossref-reader-contract",
            issn="0000-0000",
            from_pub_date="2000-01-01",
            rows=100,
        )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"{not valid json", id="json_decode_error"),
        pytest.param(b"[]", id="top_level_not_dict"),
        pytest.param(b'{"message": []}', id="message_not_dict"),
        pytest.param(b'{"message": {"items": {}}}', id="items_not_list"),
    ],
)
async def test_unreadable_payload_raises_unreadable_response(body: bytes) -> None:
    """接続は成功したが構造化できない payload (JSON decode 失敗 / envelope shape
    不正) は read 段固有の ``UnreadableResponseError`` に写る (接続境界
    ``ExternalFetchError`` とは別系統)。生 ``JSONDecodeError`` / ``AttributeError``
    を上位に漏らさない。"""
    with pytest.raises(UnreadableResponseError):
        await _fetch_body(body)


async def test_empty_items_is_success_not_unreadable() -> None:
    """正常な空 items は成功 (空列) で、unreadable に倒さない。"""
    assert await _fetch_body(b'{"message":{"items":[]}}') == []
