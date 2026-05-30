"""``mdpi/_common`` (Crossref API, Pattern R) の不変条件テスト。

このファイルが固定するのは MDPI 共通処理 **固有で他に被覆の無い** 不変条件:

- ``is_collectable_mdpi_work`` の収集スコープ真理値表 (spec 第4責務 =
  Source のスコープ宣言述語。対象外 ≠ 変換失敗 ≠ 構造的非記事。
  ``ConversionRejection`` 化も converter 移設もしない)。**scope 規則の SSoT
  はここ**。CC BY 4.0 / journal-article のみ採るという法務・選別ルールは
  converter も 系統A invariants も持たないため、純粋述語の真理値表として
  ここが唯一の不変条件所有点。
- ``to_fetched_article`` が in-scope な degenerate (DOI 欠落) を握りつぶさず
  **total** に ``FetchedArticle(url="")`` を出し、fetcher 経路で
  ``ConversionRejection`` として可視化される (spec「写像で None/drop/skip
  しない」は写像ごとに pin が要る — converter/fetcher テストは
  ``FetchedArticle`` を直接与え MDPI 写像を通らないため、ここでしか
  MDPI シームの totality を pin できない)。
- ``to_fetched_article`` が source_url を DOI canonical resolver にする写像
- collect が documented な in-scope 件数より over-filter しない
- ``works()`` には注入 ``issn`` / ``from_pub_date`` / ``rows`` (既定 20) が渡る
- ``CrossrefReader`` の ``ExternalFetchError`` は ``collect`` を素通しする

passport 業務不変条件 (at_least_one / 型許容 / 主経路型 / 永続化) は
parametrized ``test_non_rss_adapters_invariants.py`` [MDPI*] が 系統A
シートベルトとして所有 (scope 述語が旧 ``mdpi_items`` の Cat A 除外を byte
不変に再現するため自然生存)。degenerate の棄却 *理由* (``url_empty`` 等) は
converter 層 (``test_fetched_article_converter.py``) が機構非依存 SSoT として
所有し、本ファイルは理由を再検証せず「MDPI 写像が total で可視化に到達する」
リンクのみ pin する。旧 ``test_*_dropped`` (``assert items == []``) /
``count == 1`` / ``yields_passports_from_fixture`` は spec が意図的に壊す
silent-drop と系統A 重複を業務ルールとして凍結する確認重複だったため削除した。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetched_article_converter import (
    ConversionRejection,
)
from app.collection.article_acquisition.reader.crossref_reader import (
    CrossrefEntry,
    CrossrefReader,
    normalize_item,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)
from app.collection.sources.definitions.mdpi import (
    MDPIMaterialsSource,
    is_collectable_mdpi_work,
    to_fetched_article,
)
from tests.collection.sources._fixture_tools import fixture_tools
from tests.collection.sources._invariant import FetchItem, drive_source

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "mdpi_crossref.json"
)
_MATERIALS_ISSN = "1996-1944"
_DEFAULT_ROWS_PER_REQUEST = 20

# 録画標本 mdpi_crossref.json の provenance (predicate をテスト内で呼ばず
# 構成を documented 値として固定 = over-filter escape を塞ぐ):
#   item0 = CC BY 4.0 journal-article  -> in-scope (採る)
#   item1 = correction (CC BY 4.0)     -> scope-out (type)
#   item2 = 非 CC BY journal-article   -> scope-out (license)
# ゆえに in-scope 件数 = 1。
_DOCUMENTED_IN_SCOPE_COUNT = 1


def _items() -> list[dict[str, Any]]:
    raw = json.loads(_FIXTURE.read_text())
    return list(raw["message"]["items"])


def _in_scope_base() -> CrossrefEntry:
    """録画標本の in-scope item (item0 = CC BY 4.0 journal-article) を Reader
    本物の ``normalize_item`` 経由で取り、scope 述語の真理値表の基点にする。

    基点が in-scope であることを tripwire として guard (標本が将来差し替え
    られ item0 が in-scope でなくなったら明示的に落とす)。"""
    base = normalize_item(_items()[0])
    assert is_collectable_mdpi_work(base)
    return base


class _FakeCrossrefClient(CrossrefReader):
    """kwargs spy。呼出 kwargs を記録し items を本物の ``normalize_item`` で
    ``CrossrefEntry`` 列に写す (構造的 fake)。"""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self.calls: list[dict[str, Any]] = []

    async def fetch_works(
        self,
        *,
        source_name: str,
        issn: str,
        from_pub_date: str,
        rows: int,
    ) -> list[CrossrefEntry]:
        self.calls.append(
            {
                "source_name": source_name,
                "issn": issn,
                "from_pub_date": from_pub_date,
                "rows": rows,
            }
        )
        return [normalize_item(item) for item in self._items]


class _RaisingCrossrefClient(CrossrefReader):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def fetch_works(
        self,
        *,
        source_name: str,  # noqa: ARG002
        issn: str,  # noqa: ARG002
        from_pub_date: str,  # noqa: ARG002
        rows: int,  # noqa: ARG002
    ) -> list[CrossrefEntry]:
        raise self._exc


async def _drive(client: CrossrefReader) -> list[FetchItem]:
    """``MDPIMaterialsSource`` を fixture client 注入で収集 → 変換経路に通す。"""
    return await drive_source(MDPIMaterialsSource, tools=fixture_tools(crossref=client))


# ── 収集スコープ述語の真理値表 (scope 規則の SSoT) ──────────────────────
# 基点は録画標本の in-scope item。Cat A 各規則を 1 つずつ崩し False を確認。
# 閾値 (50) も regex も再エンコードせず振る舞い境界だけ見る。


def test_recorded_in_scope_item_is_collectable() -> None:
    assert is_collectable_mdpi_work(_in_scope_base()) is True


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"entry_type": "correction"}, id="non_journal_article_type"),
        pytest.param({"entry_type": None}, id="missing_type"),
        pytest.param({"license_urls": ()}, id="missing_license"),
        pytest.param(
            {"license_urls": ("https://creativecommons.org/licenses/by-nc/4.0/",)},
            id="non_cc_by_license",
        ),
        pytest.param(
            {"license_urls": ("https://creativecommons.org/licenses/by/3.0/",)},
            id="cc_by_wrong_version",
        ),
        pytest.param({"body": "too short"}, id="short_abstract"),
        pytest.param({"published": None}, id="missing_date"),
    ],
)
def test_out_of_scope_work_is_not_collectable(mutation: dict[str, Any]) -> None:
    """Cat A 各規則違反は収集スコープ外 (= ソースが意図的に採らない対象外)。

    ``cc_by_wrong_version`` は CC BY だがバージョン違い → 規則は 4.0 限定
    (法務ルール。regex を ``licenses/by/`` に緩めたら Red になる独立境界)。
    """
    entry = replace(_in_scope_base(), **mutation)
    assert is_collectable_mdpi_work(entry) is False


def test_out_of_scope_is_not_a_degenerate_failure() -> None:
    """correction は title/doi/body/date が妥当でも out-of-scope。

    収集スコープ外は変換失敗 (degenerate) ではない (spec 第4責務)。妥当な
    値を持つ correction が False になることで「対象外 ≠ 変換失敗」を固定。
    """
    valid_but_out_of_scope = replace(_in_scope_base(), entry_type="correction")
    assert valid_but_out_of_scope.title
    assert valid_but_out_of_scope.doi
    assert len(valid_but_out_of_scope.body) >= 50
    assert valid_but_out_of_scope.published is not None
    assert is_collectable_mdpi_work(valid_but_out_of_scope) is False


def test_scope_does_not_govern_doi() -> None:
    """発見オラクル: scope 述語は DOI を見ない (DOI 欠落でも in-scope)。

    DOI 欠落 in-scope = 「scope は通るが degenerate」Cat B 標本。これが
    ``to_fetched_article`` の totality 検証 (下記) の前提。``to_fetched_article``
    が「DOI 無ければ skip」と最適化したり、述語が DOI を gate し始めたら
    この行か下記が Red になる。
    """
    assert is_collectable_mdpi_work(replace(_in_scope_base(), doi=None)) is True


# ── 写像 totality (spec「写像で None/drop/skip しない」を MDPI シームで pin) ──


def test_mapping_is_total_on_in_scope_degenerate() -> None:
    """in-scope だが DOI 欠落の entry に対し写像は None/raise/skip せず
    ``FetchedArticle(url="")`` を返す (total)。

    converter/fetcher テストは ``FetchedArticle`` を直接与え MDPI 写像を
    通らないため、MDPI シームの totality はここでしか pin できない。
    """
    fa = to_fetched_article(replace(_in_scope_base(), doi=None))
    assert isinstance(fa, FetchedArticle)
    assert fa.url == ""  # 握りつぶさず空 URL を素通し (converter が可視化)


@pytest.mark.asyncio
async def test_in_scope_degenerate_surfaces_as_rejection_without_stopping_stream() -> (
    None
):
    """DOI 欠落 in-scope は黙って消えず ``ConversionRejection`` として現れ、
    他の in-scope は ``AnalyzableArticle`` のまま stream が止まらない。

    旧 ``test_missing_doi_dropped`` (``assert items == []``) が凍結していた
    silent-drop の **真の不変条件** (failure-visibility) を Red 先行で再建。
    """
    valid = _items()[0]
    no_doi = deepcopy(_items()[0])
    del no_doi["DOI"]
    items = await _drive(_FakeCrossrefClient([valid, no_doi]))
    assert any(isinstance(i, AnalyzableArticle) for i in items)  # valid 健在
    assert any(isinstance(i, ConversionRejection) for i in items)  # degenerate 可視
    assert len(items) == 2  # stream が止まらず両方到達 (片方 raise で停止しない)


# ── Source 写像 / Reader 設定 / typed-error 境界 ───────────────────────


@pytest.mark.asyncio
async def test_doi_url_used_as_source_url() -> None:
    """``to_fetched_article`` 写像: source_url は DOI canonical resolver。"""
    items = await _drive(_FakeCrossrefClient(_items()))
    assert items
    for item in items:
        assert isinstance(item, AnalyzableArticle)
        assert str(item.source_url).startswith("https://doi.org/10.3390/")


@pytest.mark.asyncio
async def test_collect_does_not_over_filter_documented_in_scope_count() -> None:
    """collect は documented な in-scope 件数 (provenance 由来) だけ yield する。

    predicate-unit が正でも collect が誤条件で over-filter (journal-article
    を 1 本落とす) と 系統A も真理値表も件数を見ないため escape する。
    期待値は predicate をテスト内で呼ばず標本 provenance から固定。
    """
    items = await _drive(_FakeCrossrefClient(_items()))
    analyzable = [i for i in items if isinstance(i, AnalyzableArticle)]
    assert len(analyzable) == _DOCUMENTED_IN_SCOPE_COUNT


@pytest.mark.asyncio
async def test_client_kwargs_carry_issn_lookback_rows() -> None:
    fake = _FakeCrossrefClient([])
    await _drive(fake)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["source_name"] == "MDPI Materials"
    assert call["issn"] == _MATERIALS_ISSN
    assert call["rows"] == _DEFAULT_ROWS_PER_REQUEST
    # from_pub_date は date.isoformat() 由来の "YYYY-MM-DD" 文字列
    assert isinstance(call["from_pub_date"], str)
    assert len(call["from_pub_date"]) == 10


@pytest.mark.asyncio
async def test_non_recoverable_error_propagates_through_collect() -> None:
    client = _RaisingCrossrefClient(
        FetchAccessDeniedError(status_code=403, reason="forbidden")
    )
    with pytest.raises(FetchAccessDeniedError):
        await _drive(client)


@pytest.mark.asyncio
async def test_recoverable_error_propagates_through_collect() -> None:
    client = _RaisingCrossrefClient(
        FetchOriginServerError(status_code=500, reason="internal_error")
    )
    with pytest.raises(FetchOriginServerError):
        await _drive(client)
