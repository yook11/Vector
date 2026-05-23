"""普遍 Reader 役割契約の **発見オラクル** (全機構横断・凍らせた実標本)。

C1 ([test_rss_reader_contract.py](./test_rss_reader_contract.py)) は RSS Reader
*固有の性質* (title 平文化 / published tz-aware / body 非掃除 / no-drop) を見る。
本テストはその姉妹で、機構非依存の **役割契約** を見る:

  *機構の Reader entrypoint は、録画した実 transport を食わせると、その機構
  固有の typed Entry 箱の列を返す。生 transport 型 (dict / bytes) を Source に
  そのまま漏らさない。*

これは spec (`specs/Refactor source and fetcher.md`) の Reader 役割
(R1 録画実 transport→機構 typed Entry / R5 Entry 箱は機構ごと・frozen・
記述用) を機構横断の一様 assert に落としたもの。R2 (Reader が parse/decode/
Entry 抽出を所有) は系として従う: typed Entry を返せるなら抽出は Reader 側に
ある。

**射程は typed Entry の "形" だけ**。no-drop / per-item no-raise (R3) や
typed-error 境界 (R4) は本オラクルでは見ない。それらは別テストの責務:
RSS の no-drop は C1、parse/transport error 境界は ``test_rss_reader.py``、
各機構は Reader 実体化時に per-mechanism 契約で個別に足す。発見オラクルを
R1/R5 に絞るのは判別力を「typed Entry 箱の有無」に集中させるため
(scope creep は判別をぼかす)。

**発見オラクルであって確認テストではない** ([[feedback_test_first_discovery_not
_confirmatory]])。同一の一様 assert を全機構に当て、現コードで RSS だけ緑に
なり、HN / Crossref / raw は ``list[dict]`` / ``bytes`` を返すため赤になる。
**その赤が spec 未実現の発見** = strangler バックログの SSoT であり、修正対象を
手で列挙したものではない。

``invoke`` は **repointable な strangler seam** であって「現 transport client の
メソッド ≡ その機構の Reader」という主張ではない。今それを ``search_recent_
stories`` / ``works`` / ``RawHttpClient.fetch`` に刺しているのは、現状その入口
しか無く→生 transport 型が漏れる→だから赤、を示す **暫定** にすぎない。
機構別 strangler で本物の Reader 入口 (例 ``HackerNewsReader().fetch``) が
できたら、その機構の「赤→緑」遷移は次の3手順で行う:

1. Reader を実体化し typed Entry 箱を返させる。
2. 当該 ``_Mechanism.invoke`` を **本物の Reader へ向け直す** (transport client
   メソッドへの暫定刺しを捨てる)。
3. 当該行の ``xfail`` marks を外す (行を恒久シートベルト化)。

赤は ``xfail(strict=True)`` で記録する。意味は二つ:

- 既知の未実現 (バックログ項目) を suite が赤として宣言するが hard failure に
  しない (``pytest -x`` を止めない / シートベルトの passed 数を汚さない)。
- 上記手順で Reader を実体化し ``invoke`` を向け直すと行が緑になり、strict
  xfail は XPASS を **失敗** として報告する。これが marks 除去を強制する装置
  (外さない限り CI が赤を出し続ける)。緑化の取りこぼしが構造的に起きない。

**赤の triage (厳守) — 正規操作は次の3つのみ**:

1. **in-place 昇格 → marks 除去**: 機構の既存 entrypoint が同一 callable の
   まま typed Entry を返すよう昇格した場合 (HN/Crossref 型)。XPASS strict
   failure が出る = Reader が実装された合図。対応は *当該 marks の除去*
   (行を恒久シートベルト化) のみ。
2. **新クラスへ移動 → invoke/module 貼り替え + marks 除去** (Conflict 2):
   Reader entrypoint が**別の新クラス**へ移った場合 (raw → ``SitemapReader``
   / ``HtmlListingReader`` 型。元 ``RawHttpClient.fetch`` は純 transport の
   bytes のまま正しく残るため、貼り替えない限り当該行は**永久に XPASS せず
   strict-xfail の強制装置が死ぬ**)。strangler step 内で当該 ``_Mechanism``
   の ``invoke=`` / ``module=`` を新 Reader へ**貼り替え**、同時に marks を
   外す。これは下記「assert 緩め」とも、Step 4 の改名由来 ``module``/_MOD
   文字列追従とも**別物**の正規操作。
3. **真の over-claim → assert 是正**: assert が契約を超過していたと判明した
   場合のみ assert 自体を是正する。「現コードの偶発出力に合わせて緩める」のと
   は別物。

**禁止**: 上記 2 (貼り替え) を口実にした、あるいはそれ以外の理由での
**一様 assert の緩め**で行を通すこと (発見オラクルの自殺 = 確認テストへの
逆戻り)。貼り替えは entrypoint の所在を正すだけで assert は不変に保つ。

**raw (sitemap / HTML listing) も同じ typed Entry 契約から免除しない**。spec の
「Entry 型省略は狭い例外」は *Entry 型名の省略* であって *parse を Source に
残してよい許可ではない*。``SitemapReader`` / ``HtmlListingReader`` は frozen
dataclass Entry を返さねば緑にならない (生 bytes を Source に渡し続ける限り
赤のまま)。「raw は spec 例外だから」と raw 行を緩めるのは禁止。

C1 と同じ契約テスト規律: 差し替えるのは transport seam
(``make_safe_async_client``) **のみ**。parse / decode / Entry 抽出は機構実装の
本物が動く。本テストは各機構の **公開 entrypoint** と ``dataclasses`` の構造
事実しか知らず、Reader 内臓を一切 import しない。標本は各機構 transport の
**形** の代表で、録り直しは「何が壊れるかを意図して確かめる人間の作業」
(provenance 規律であって CI flake ではない)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.article_collection.reader.algolia_hn_reader import HackerNewsReader
from app.collection.article_collection.reader.crossref_reader import CrossrefReader
from app.collection.article_collection.reader.html_listing_reader import (
    HtmlListingReader,
)
from app.collection.article_collection.reader.rss_reader import RssReader
from app.collection.article_collection.reader.sitemap_reader import SitemapReader

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures (C1 と同一)
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"

_URL = "https://example.com/feed"
_NAME = "reader-role-contract"


@dataclass(frozen=True)
class _Mechanism:
    """1 機構の (録画標本 + transport seam の所在 + Reader 候補 entrypoint)。

    ``invoke`` は transport を patch した文脈の中で「現状その機構の入口」を
    呼ぶ coroutine factory であり、本物の Reader ができたら向け直す seam
    (module docstring の3手順)。entrypoint は機構ごとに名前 / kw が違うが、
    それを呑むのは ``invoke`` の責務で、契約 assert は一様に保つ。
    """

    name: str
    module: str  # make_safe_async_client を import している = patch 対象
    fixture: str  # 録画した実 transport バイト列
    invoke: Callable[[], Awaitable[object]]


_MECHANISMS: list[_Mechanism] = [
    _Mechanism(
        name="rss",
        module="app.collection.article_collection.reader.rss_reader",
        fixture="nist_rss.xml",
        invoke=lambda: RssReader().fetch(
            endpoint_url=_URL, source_name=_NAME, parse_mode="bytes"
        ),
    ),
    _Mechanism(
        name="hacker_news",
        module="app.collection.article_collection.reader.algolia_hn_reader",
        fixture="hacker_news_hits.json",
        invoke=lambda: HackerNewsReader().search_recent_stories(
            source_name=_NAME,
            min_points=0,
            window_seconds=10**12,
            hits_per_page=100,
        ),
    ),
    _Mechanism(
        name="crossref",
        module="app.collection.article_collection.reader.crossref_reader",
        fixture="mdpi_crossref.json",
        invoke=lambda: CrossrefReader().fetch_works(
            source_name=_NAME,
            issn="0000-0000",
            from_pub_date="2000-01-01",
            rows=100,
        ),
    ),
    _Mechanism(
        # SitemapReader は RawHttpClient を wrap するため transport seam は
        # raw_http_client に在る → module= は不変、invoke= のみ新 Reader へ
        # 貼り替え (triage 操作2 の合成版。module 移動を伴わない)。
        name="raw_sitemap",
        module="app.collection.article_collection.tools.raw_http_client",
        fixture="anthropic_sitemap.xml",
        invoke=lambda: SitemapReader().fetch(url=_URL, source_name=_NAME),
    ),
    _Mechanism(
        name="raw_html_listing",
        module="app.collection.article_collection.tools.raw_http_client",
        fixture="ornl_listing.html",
        # detail_link_xpath は Source 宣言値。fixture は ORNL の実 listing
        # なのでこの値で抽出する (HN min_points 等と同じ機構別 invoke 引数)。
        invoke=lambda: HtmlListingReader().fetch(
            url=_URL,
            source_name=_NAME,
            detail_link_xpath='//a[starts-with(@href, "/news/")]',
        ),
    ),
]

# 全機構 Reader 実体化済 = 既知赤なし。
# - hacker_news: Step 1 で実体化 (HackerNewsReader が list[HackerNewsEntry])。
# - crossref:    Step 2 で実体化 (CrossrefReader が list[CrossrefEntry]。
#                in-place 昇格 = 同一 callable のため invoke 向け直し不要)。
# - raw_sitemap / raw_html_listing: Step 3 で SitemapReader /
#                HtmlListingReader へ実体化。新 Reader は RawHttpClient を
#                wrap するため module= (seam の所在) は不変で invoke= のみ
#                貼り替え (triage 操作2 の合成版)。
_NOT_YET_A_READER: set[str] = set()


def _params() -> list[Any]:
    out: list[Any] = []
    for m in _MECHANISMS:
        marks = (
            pytest.mark.xfail(
                reason=(
                    f"{m.name}: Reader 未実体化 (生 transport 型を Source に漏らす)。"
                    " strangler で typed Entry 化し invoke を向け直したら marks を外す"
                ),
                strict=True,
            )
            if m.name in _NOT_YET_A_READER
            else ()
        )
        out.append(pytest.param(m, marks=marks, id=m.name))
    return out


async def _run(m: _Mechanism) -> object:
    """録画実 transport を当該機構の Reader 候補 entrypoint に流す。

    差し替えるのは ``make_safe_async_client`` のみ。HTTP status / json /
    bytes 取り出し / parse は機構実装の本物が動く。
    """
    raw = (_FIXTURES_DIR / m.fixture).read_bytes()
    response = httpx.Response(
        status_code=200,
        content=raw,
        request=httpx.Request("GET", _URL),
    )

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{m.module}.make_safe_async_client", _fake_safe_client):
        return await m.invoke()


@pytest.mark.parametrize("m", _params())
async def test_reader_returns_mechanism_typed_entry_boxes(m: _Mechanism) -> None:
    """Reader は録画実 transport から機構固有 typed Entry 箱の列を返す。

    R1: 単一の生 transport 塊でなく entry の列。
    R5: 各 entry は機構固有の frozen dataclass 箱 (生 transport 型 dict/bytes/
        str でない / enum でない)。
    """
    result = await _run(m)

    # R1: Reader は entry の列を返す (bytes 1 塊や生 dict 列でない)
    assert isinstance(result, list), (m.name, type(result))
    assert result, m.name  # 録画標本は最低1件 (空シートベルト防止)
    for e in result:
        # R5: 機構固有の frozen dataclass 箱
        assert is_dataclass(e) and not isinstance(e, type), (m.name, type(e))
        assert not isinstance(e, dict | bytes | str), (m.name, type(e))
        assert not isinstance(e, Enum), (m.name, type(e))
        assert e.__dataclass_params__.frozen, (m.name, type(e))
