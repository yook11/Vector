"""``TechCrunchAdapter`` の per-source 単体テスト (HTTP 非依存)。

実 RSS fixture を ``_FakeRssParser`` 経由で食わせ、Adapter が body 候補を
持たない ``FetchedArticle`` を yield することと、``ArticleFetcher`` 経由で
``IncompleteArticle`` のみが yield されることを構造的に検証する。

検証観点:

- ``collect()`` が yield する全 ``FetchedArticle`` で ``body is None``
- title / url は entry から正しく渡る (空でない fixture を前提)
- ``ArticleFetcher`` 経由で全 passport が ``IncompleteArticle`` (Ready 混入 0)
- ``NAME`` / ``ENDPOINT_URL`` が class attr として読める
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.techcrunch import TechCrunchAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


class _FakeRssParser:
    """``RssParser`` の構造的 fake。fixture を feedparser で読み、
    ``normalize_entry`` を通して本番経路と同じ ``RssEntry`` を返す。"""

    def __init__(self, fixture_filename: str) -> None:
        self._fixture_filename = fixture_filename

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,  # user_agent / timeout 等の将来引数追加に耐える
    ) -> list[RssEntry]:
        path = _FIXTURES_DIR / self._fixture_filename
        feed = feedparser.parse(path.read_bytes())
        return [normalize_entry(raw) for raw in feed.entries]


async def _collect_fetched(adapter: TechCrunchAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_collect_yields_fetched_articles_with_none_body() -> None:
    """TC は RSS body を信用しないため、Adapter は常に ``body=None`` を出す。"""
    adapter = TechCrunchAdapter(parser=_FakeRssParser("techcrunch_rss.xml"))  # type: ignore[arg-type]

    items = await _collect_fetched(adapter)

    assert items, "fixture must yield at least one FetchedArticle"
    assert all(isinstance(item, FetchedArticle) for item in items)
    assert all(item.body is None for item in items)


async def test_collect_propagates_title_and_url_from_entries() -> None:
    """Adapter は entry の title / url を空 str も含めてそのまま渡す責務に閉じる。
    空 str を drop に変換するのは ``passport_builder`` 側の責務。"""
    adapter = TechCrunchAdapter(parser=_FakeRssParser("techcrunch_rss.xml"))  # type: ignore[arg-type]

    items = await _collect_fetched(adapter)

    # 1 件以上は title / url が揃った "実 entry" であること (fixture の sanity check)
    assert any(item.title and item.url.startswith("https://") for item in items)


async def test_article_fetcher_yields_incomplete_only() -> None:
    """``ArticleFetcher(TechCrunchAdapter())`` 経路で yield される passport は
    全 ``IncompleteArticle`` (Ready 経路への昇格は構造的に発生しない)。"""
    adapter = TechCrunchAdapter(parser=_FakeRssParser("techcrunch_rss.xml"))  # type: ignore[arg-type]
    fetcher = ArticleFetcher(adapter)

    passports = [item async for item in fetcher.fetch(source_id=1)]

    assert passports
    assert all(isinstance(p, IncompleteArticle) for p in passports)


def test_exposes_name_and_endpoint_url() -> None:
    assert TechCrunchAdapter.NAME == "TechCrunch"
    assert TechCrunchAdapter.ENDPOINT_URL == "https://techcrunch.com/feed/"
