"""``VentureBeatSource`` の per-source 単体テスト (HTTP 非依存, P2-D)。

P2-D で identity / 補完方針は ``XxxSource`` の ``ClassVar``、取得手順は
``collect(tools)`` classmethod になった。fixture を ``ReaderTools.rss`` 差し替え
(``fixture_tools``) 経由で食わせ、Source の ``FetchedArticle`` field 内容と、
収集 → 変換の本番経路の passport 出力を検証する (identity 固定は
test_source_adapter_profiles に集約)。
"""

from __future__ import annotations

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_BODY_MIN_LENGTH
from app.collection.domain.observed_article import ObservedArticle
from app.collection.sources.definitions.venturebeat import VentureBeatSource
from tests.collection.sources._fixture_tools import fixture_tools
from tests.collection.sources._invariant import FetchItem, drive_source


async def _collect_fetched(fixture: str) -> list[FetchedArticle]:
    tools = fixture_tools(rss_fixture=fixture)
    return [item async for item in fetch_articles(VentureBeatSource, tools)]


async def _passports(fixture: str) -> list[FetchItem]:
    return await drive_source(
        VentureBeatSource, tools=fixture_tools(rss_fixture=fixture)
    )


async def test_collect_yields_fetched_articles_with_body_from_full_rss() -> None:
    items = await _collect_fetched("venturebeat_rss.xml")

    assert items, "full fixture must yield at least one FetchedArticle"
    assert all(isinstance(item, FetchedArticle) for item in items)
    assert any(
        item.body is not None and len(item.body) >= ARTICLE_BODY_MIN_LENGTH
        for item in items
    )


async def test_collect_yields_short_body_from_teaser_rss() -> None:
    """teaser fixture では body 候補が短く、Ready 昇格条件を満たさない。"""
    items = await _collect_fetched("venturebeat_teaser_rss.xml")

    assert items
    assert all(
        item.body is None or len(item.body) < ARTICLE_BODY_MIN_LENGTH for item in items
    )


async def test_collect_convert_yields_ready_for_full_rss() -> None:
    """full fixture が最低 1 件の ``AnalyzableArticle`` を yield する (主経路)。"""
    passports = await _passports("venturebeat_rss.xml")

    assert any(isinstance(p, AnalyzableArticle) for p in passports)


async def test_collect_convert_falls_back_to_incomplete_for_teaser_rss() -> None:
    """teaser-only fixture では全 entry が ``ObservedArticle`` に落ちる。"""
    passports = await _passports("venturebeat_teaser_rss.xml")

    assert passports
    assert all(isinstance(p, ObservedArticle) for p in passports)
