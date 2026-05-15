"""``VentureBeatAdapter`` の per-source 単体テスト (HTTP 非依存)。

実 RSS fixture を ``_FakeRssParser`` 経由で食わせ、Adapter が出す
``FetchedArticle`` の field 内容と、``ArticleFetcher(VentureBeatAdapter())``
を通したときの passport 出力を検証する。

検証観点:

- ``collect()`` が fixture の entry 数だけ ``FetchedArticle`` を yield する
- body 候補が ``_pick_body`` + ``_strip_html`` 経由で組まれる (full fixture)
- teaser RSS では body が 50 chars 未満になり、Adapter は ``str`` を yield する
  (Ready / Incomplete の分岐は ``passport_builder`` 側の責務)
- ``ArticleFetcher`` 経由で full fixture → ``ReadyForArticle`` を最低 1 件
- ``ArticleFetcher`` 経由で teaser fixture → 全 entry が ``IncompleteArticle``
  (body 短い entry の Ready→Incomplete fallback 経路を構造的に固定)
- ``NAME`` / ``ENDPOINT_URL`` が class attr として読める
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.article.domain.article import (
    _ARTICLE_BODY_MIN_LENGTH,
    ReadyForArticle,
)
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry
from app.collection.fetchers.venturebeat import VentureBeatAdapter
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


async def _collect_fetched(adapter: VentureBeatAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_collect_yields_fetched_articles_with_body_from_full_rss() -> None:
    adapter = VentureBeatAdapter(parser=_FakeRssParser("venturebeat_rss.xml"))  # type: ignore[arg-type]

    items = await _collect_fetched(adapter)

    assert items, "full fixture must yield at least one FetchedArticle"
    assert all(isinstance(item, FetchedArticle) for item in items)
    # full fixture の少なくとも 1 件は Ready 構築可能な body 長を持つ
    assert any(
        item.body is not None and len(item.body) >= _ARTICLE_BODY_MIN_LENGTH
        for item in items
    )


async def test_collect_yields_short_body_from_teaser_rss() -> None:
    """teaser fixture では body 候補が短く、Ready 昇格条件を満たさない。
    Adapter 自身は判定せず、長さがある body を yield する。"""
    adapter = VentureBeatAdapter(parser=_FakeRssParser("venturebeat_teaser_rss.xml"))  # type: ignore[arg-type]

    items = await _collect_fetched(adapter)

    assert items
    assert all(
        item.body is None or len(item.body) < _ARTICLE_BODY_MIN_LENGTH for item in items
    )


async def test_article_fetcher_yields_ready_for_full_rss() -> None:
    """``ArticleFetcher(VentureBeatAdapter())`` 経路で full fixture が
    最低 1 件の ``ReadyForArticle`` を yield することを確認する (主経路)。"""
    adapter = VentureBeatAdapter(parser=_FakeRssParser("venturebeat_rss.xml"))  # type: ignore[arg-type]
    fetcher = ArticleFetcher(adapter)

    passports = [item async for item in fetcher.fetch(source_id=1)]

    assert any(isinstance(p, ReadyForArticle) for p in passports)


async def test_article_fetcher_falls_back_to_incomplete_for_teaser_rss() -> None:
    """teaser-only fixture では全 entry が ``IncompleteArticle`` に落ちる。
    builder の Ready→Incomplete fallback を Adapter 経路でも保つ構造的保証。"""
    adapter = VentureBeatAdapter(parser=_FakeRssParser("venturebeat_teaser_rss.xml"))  # type: ignore[arg-type]
    fetcher = ArticleFetcher(adapter)

    passports = [item async for item in fetcher.fetch(source_id=1)]

    assert passports
    assert all(isinstance(p, IncompleteArticle) for p in passports)


def test_exposes_name_and_endpoint_url() -> None:
    assert VentureBeatAdapter.NAME == "VentureBeat"
    assert VentureBeatAdapter.ENDPOINT_URL == "https://venturebeat.com/feed"
