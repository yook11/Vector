"""``VentureBeatSource`` の per-source 単体テスト (HTTP 非依存, P2-D)。

P2-D で identity / 補完方針は ``XxxSource`` の ``ClassVar``、取得手順は
``collect(tools)`` classmethod になった。fixture を ``FetchTools.rss`` 差し替え
(``fixture_tools``) 経由で食わせ、Source の ``FetchedArticle`` field 内容と、
``ArticleFetcher`` 経由の passport 出力を検証する (identity 固定は
test_source_adapter_profiles に集約)。
"""

from __future__ import annotations

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_BODY_MIN_LENGTH
from app.collection.domain.observed_article import ObservedArticle
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.venturebeat import VentureBeatSource
from tests.collection.fetchers._fixture_tools import fixture_tools


async def _collect_fetched(fixture: str) -> list[FetchedArticle]:
    tools = fixture_tools(rss_fixture=fixture)
    return [item async for item in VentureBeatSource.collect(tools)]


def _fetcher(fixture: str) -> ArticleFetcher:
    return ArticleFetcher(VentureBeatSource, tools=fixture_tools(rss_fixture=fixture))


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


async def test_article_fetcher_yields_ready_for_full_rss() -> None:
    """full fixture が最低 1 件の ``AnalyzableArticle`` を yield する (主経路)。"""
    passports = [item async for item in _fetcher("venturebeat_rss.xml").fetch(1)]

    assert any(isinstance(p, AnalyzableArticle) for p in passports)


async def test_article_fetcher_falls_back_to_incomplete_for_teaser_rss() -> None:
    """teaser-only fixture では全 entry が ``ObservedArticle`` に落ちる。"""
    passports = [item async for item in _fetcher("venturebeat_teaser_rss.xml").fetch(1)]

    assert passports
    assert all(isinstance(p, ObservedArticle) for p in passports)
