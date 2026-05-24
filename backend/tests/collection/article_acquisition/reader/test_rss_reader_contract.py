"""RSS Reader の契約テスト (凍らせた実標本 × 性質)。

``RssReader`` (= RSS Reader) が後段 (Source 写像) に立てている約束を、
**実在の feed を一度手で録画した固定バイト列**に対して確かめる。

契約は **Reader の公開メソッド ``RssReader.fetch`` を通して**確かめる。
差し替えるのは HTTP transport (``make_safe_async_client``) **のみ**で、
parse / decode / Entry 抽出は Reader 内部で本物が動く。spec 上 feedparser や
``normalize_entry`` は「Reader が持つもの (parse/decode/Entry 抽出)」=
**Reader の内臓**であり、本テストはそれを一切 import しない。テストが知るのは
``RssReader.fetch`` と ``RssEntry`` だけ。これにより Reader の内部合成が
変わっても (spec の段階的 Reader 抽出) 契約が保たれる限り本テストは生き、
逆に内臓を手で組み直して自前配線を確かめる愚を犯さない。

既存 ``test_rss_reader.py::TestRssReaderFetch`` は ``fetch`` を呼ぶが
**feedparser も mock** しており「実 feedparser が実 feed に何を返すか」を
誰も確かめていない。本テストはその差分 = feedparser mock を外し、録画した
実バイトを transport に流す。

見るのは feed の **中身** ではなく Reader が永遠に守ると約束した **性質**:

- title は markup を含まず実体参照は decode 済 (平文化済)
- body (summary) は掃除しない (Reader は素材を広いまま手渡す。意味づけは後段)
- published は tz-aware か None (tz-naive を作らない)
- 値が欠落しただけの候補も drop / raise せず Entry として通す (spec: 判定は
  後段の責務)

litmus: 標本を録り直して赤くなるなら中身を見ていた = 間違い。本テストは
Reader が出力 ``RssEntry`` に保証する性質しか見ないため、feed の文面が
変わっても赤くならない。

**赤が出たときの triage (厳守)**: red を ``rss_reader.py`` の修正 (実挙動が
真の契約に反する) か、assert の over-claim 認定 (契約を超過していた) の
どちらかで解消する。**現コードの偶発出力に合わせて assert を緩めることは
禁止** (それは合成テストの原罪の再犯)。

層の境界: 本テストは RssEntry の Reader 契約のみを見る。下流の passport 型は
``test_rss_adapters_invariants.py`` (Source+converter, source 別)、normalize の
分岐ロジック (guid 2048 切詰等) は ``test_rss_reader.py::TestNormalizeEntry``
(合成 unit) が見る。赤の ``[mic_rdf.xml]`` は「MIC ソースが壊れた」でなく
「共有 Reader が RDF のこの実構造を捌けない」と読む。修正は唯一の
``rss_reader.py`` に落とす。

標本の出所 (provenance) と鮮度: 各 fixture はある時点の実 feed の標本で、
feed の **形** の代表として選ぶ (RSS 2.0 / Atom / RDF)。外部 feed が形式を
変えたときの録り直しは「何が壊れるかを意図して確かめる人間の作業」であって
CI のランダム赤ではない。本契約は標本の鮮度までしか保証しない。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.article_acquisition.reader.rss_reader import RssEntry, RssReader

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"

# transport を差し替える対象モジュール (Reader 実装の所在)。
_MOD = "app.collection.article_acquisition.reader.rss_reader"

# feed の「形」の代表。各標本が特定 property の失敗モードを実際に踏む:
#   nist_rss.xml          RSS 2.0 / <title> に &amp;       -> 平文化を非空虚に
#   the_register_atom.xml Atom / summary に markup・空 title -> body 非掃除 / no-drop
#   mic_rdf.xml           RDF / Shift_JIS / dc:date +09:00  -> tz-aware を非空虚に
_SHAPE_FIXTURES = ["nist_rss.xml", "the_register_atom.xml", "mic_rdf.xml"]


async def _reader_entries(fixture: str) -> list[RssEntry]:
    """本物の ``RssReader().fetch`` を録画実バイトで走らせる。

    差し替えるのは HTTP transport (``make_safe_async_client``) のみ。
    feedparser / decode / Entry 抽出は Reader 内部で本物が動く。
    """
    raw = (_FIXTURES_DIR / fixture).read_bytes()
    response = httpx.Response(
        status_code=200,
        content=raw,
        request=httpx.Request("GET", "https://example.com/feed"),
    )

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        # parse_mode="bytes": feedparser に encoding sniff を委ね Shift_JIS も通す
        return await RssReader().fetch(
            endpoint_url="https://example.com/feed",
            source_name="contract-test",
            parse_mode="bytes",
        )


@pytest.mark.parametrize("fixture", _SHAPE_FIXTURES)
async def test_title_has_no_markup_or_undecoded_entities(fixture: str) -> None:
    """title は tag を含まず実体参照は decode 済 (平文化済)。"""
    entries = await _reader_entries(fixture)
    assert entries  # Reader は標本から最低1件を出す (空シートベルト防止)
    for e in entries:
        assert "<" not in e.title and ">" not in e.title, e.title
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#"):
            assert entity not in e.title, (entity, e.title)


@pytest.mark.parametrize("fixture", _SHAPE_FIXTURES)
async def test_published_is_tz_aware_or_none(fixture: str) -> None:
    """published は tz-aware か None。tz-naive は作らない。"""
    entries = await _reader_entries(fixture)
    assert entries
    for e in entries:
        assert e.published is None or e.published.tzinfo is not None


async def test_reader_does_not_strip_markup_from_body() -> None:
    """body に markup を持つ標本で Reader が summary を strip しない。

    Reader が良かれと summary を掃除すると後段の前提が静かに崩れる。
    ``the_register_atom.xml`` は ``<summary>`` が ``&lt;p&gt;...&lt;/p&gt;``
    = decode 後 ``<p>...</p>`` を持つ「body が markup を持つ」形の代表。
    summary に markup が残る = Reader が踏み出していない。feed が平文 body に
    変わったら代表を選び直す (provenance 規律であって CI flake ではない)。
    """
    entries = await _reader_entries("the_register_atom.xml")
    assert any("<" in (e.summary or "") for e in entries)


async def test_reader_passes_through_degenerate_entry_without_dropping() -> None:
    """値が欠落しただけの候補を Reader は drop / raise せず Entry として通す。

    判定は後段 (converter) の責務 (spec)。``the_register_atom.xml`` は
    空 ``<title>`` の well-formed entry を含み、それが Reader 出力に
    現れることで no-drop を公開面から固定する。
    """
    entries = await _reader_entries("the_register_atom.xml")
    assert any(e.title == "" for e in entries)
