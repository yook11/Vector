"""``TechCrunchSource`` の per-source 単体テスト (HTTP 非依存, P2-D)。

P2-D で identity / 補完方針は ``XxxSource`` の ``ClassVar``、取得手順は
``collect(tools)`` classmethod になった。実 RSS fixture を ``FetchTools.rss``
差し替え (``fixture_tools``) 経由で食わせ、Source が body 候補を持たない
``FetchedArticle`` を yield し、``ArticleFetcher`` 経由で ``ObservedArticle``
のみが yield されることを検証する (identity 固定は
test_source_adapter_profiles に集約)。
"""

from __future__ import annotations

from app.collection.domain.observed_article import ObservedArticle
from app.collection.source_fetch.article_fetcher import ArticleFetcher
from app.collection.source_fetch.errors import ConversionReason
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.fetched_article_converter import ConversionRejection
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from tests.collection.fetchers._fixture_tools import fixture_tools
from tests.collection.fetchers._invariant import passports_only

_FIXTURE = "techcrunch_rss.xml"


async def _collect_fetched() -> list[FetchedArticle]:
    tools = fixture_tools(rss_fixture=_FIXTURE)
    return [item async for item in TechCrunchSource.collect(tools)]


async def test_collect_yields_fetched_articles_with_none_body() -> None:
    """TC は RSS body を信用しないため、Source は常に ``body=None`` を出す。"""
    items = await _collect_fetched()

    assert items, "fixture must yield at least one FetchedArticle"
    assert all(isinstance(item, FetchedArticle) for item in items)
    assert all(item.body is None for item in items)


async def test_collect_propagates_title_and_url_from_entries() -> None:
    """Source は entry の title / url を空 str も含めてそのまま渡す責務に閉じる。"""
    items = await _collect_fetched()

    assert any(item.title and item.url.startswith("https://") for item in items)


async def test_article_fetcher_yields_incomplete_only() -> None:
    """``ArticleFetcher`` 経路で変換成功した passport は全 ``ObservedArticle``
    (TC は body 不信用のため Ready 経路への昇格は構造的に発生しない)。"""
    fetcher = ArticleFetcher(
        TechCrunchSource, tools=fixture_tools(rss_fixture=_FIXTURE)
    )

    items = [item async for item in fetcher.fetch(source_id=1)]
    passports = passports_only(items)

    assert passports
    assert all(isinstance(p, ObservedArticle) for p in passports)


async def test_article_fetcher_surfaces_empty_title_entry_as_rejection() -> None:
    """空 title entry は握りつぶさず ``ConversionRejection`` で表に出る。

    旧 ``try_build_passport`` は ``None`` で静かに drop していた fixture 内の
    空 title entry が、変換不能として理由付きで可視化される (故障の見える化)。
    """
    fetcher = ArticleFetcher(
        TechCrunchSource, tools=fixture_tools(rss_fixture=_FIXTURE)
    )

    items = [item async for item in fetcher.fetch(source_id=1)]
    rejections = [i for i in items if isinstance(i, ConversionRejection)]

    assert len(rejections) == 1
    assert rejections[0].error.conversion_reason is ConversionReason.MISSING_TITLE
