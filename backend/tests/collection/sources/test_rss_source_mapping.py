"""RSS取得入口の写像と、未移行ESAの写像が材料を裁かないことを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.rss_reader import RssEntry, RssReader
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.article_limits import ARTICLE_BODY_MAX_LENGTH
from app.collection.sources.definitions.esa import (
    to_fetched_article as esa_to_fetched_article,
)
from app.collection.sources.definitions.the_register import TheRegisterSource
from app.collection.sources.definitions.venturebeat import VentureBeatSource
from app.collection.sources.rss_acquisition import RssSource

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

# 固有URL変換以外の入力では両ソースが同じ材料通過契約を持つ。
_SOURCES = [VentureBeatSource, TheRegisterSource]


def make_rss_entry(**overrides: object) -> RssEntry:
    """全フィールドに健全な既定値を入れた ``RssEntry``。検証対象だけ override。"""
    base: dict = {
        "link": "https://example.com/articles/hello",
        "title": "Hello World",
        "guid": "guid-1",
        "published": _PUBLISHED,
        "summary": "<p>short summary</p>",
        "content_encoded": "<p>full content encoded body</p>",
        "tags": (),
        "raw_published": "Fri, 01 May 2026 12:00:00 GMT",
        "raw_updated": None,
    }
    return RssEntry(**(base | overrides))


async def _map(source: RssSource, entry: RssEntry) -> FetchedArticle:
    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = [entry]
    items = [item async for item in fetch_articles(source, ReaderTools(rss=reader))]
    assert len(items) == 1
    return items[0]


@pytest.mark.asyncio
async def test_venturebeat_picks_longer_content_encoded_as_body() -> None:
    entry = make_rss_entry(
        content_encoded="<p>" + "A" * 100 + "</p>", summary="<p>short</p>"
    )
    assert (await _map(VentureBeatSource, entry)).body == "A" * 100


@pytest.mark.asyncio
async def test_venturebeat_picks_longer_summary_as_body() -> None:
    entry = make_rss_entry(
        content_encoded="<p>short</p>", summary="<p>" + "B" * 100 + "</p>"
    )
    assert (await _map(VentureBeatSource, entry)).body == "B" * 100


@pytest.mark.asyncio
async def test_venturebeat_strips_html_tags_and_decodes_entities_in_body() -> None:
    entry = make_rss_entry(
        content_encoded="<p>Hello &amp; <b>world</b></p>", summary=""
    )
    body = (await _map(VentureBeatSource, entry)).body
    assert body == "Hello & world"


@pytest.mark.asyncio
async def test_venturebeat_returns_none_body_when_both_sources_empty() -> None:
    """body 候補が無くても None を返すだけで raise / drop しない。"""
    entry = make_rss_entry(content_encoded=None, summary=None)
    result = await _map(VentureBeatSource, entry)
    assert isinstance(result, FetchedArticle)
    assert result.body is None


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.asyncio
async def test_mapping_does_not_reject_empty_title(source: type) -> None:
    """空 title は写像で裁かず空 str のまま FetchedArticle に載る。"""
    result = await _map(source, make_rss_entry(title=""))
    assert isinstance(result, FetchedArticle)
    assert result.title == ""


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize(
    "raw_url",
    [
        "http://127.0.0.1/secret",
        "javascript:alert(1)",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
@pytest.mark.asyncio
async def test_mapping_passes_raw_url_through_without_canonicalize_or_validation(
    source: type, raw_url: str
) -> None:
    """canonicalize / SSRF 検証は converter の責務。写像は生 URL を素通し。"""
    result = await _map(source, make_rss_entry(link=raw_url))
    assert result.url == raw_url


@pytest.mark.asyncio
async def test_venturebeat_passes_short_body_through_without_min_length_judgement() -> (
    None
):
    """短いが非空の body は短さで None 化しない (空→None は strip 結果と別)。"""
    entry = make_rss_entry(content_encoded="x", summary=None)
    assert (await _map(VentureBeatSource, entry)).body == "x"


@pytest.mark.asyncio
async def test_venturebeat_passes_oversized_body_through_without_truncation() -> None:
    """converter の最大長 cap を写像は適用しない (素通し)。"""
    huge = "y" * (ARTICLE_BODY_MAX_LENGTH + 1)
    entry = make_rss_entry(content_encoded="<p>" + huge + "</p>", summary=None)
    assert len((await _map(VentureBeatSource, entry)).body or "") == len(huge)


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.asyncio
async def test_mapping_passes_published_none_through(source: type) -> None:
    """published 不在は捏造せず None のまま素通し。"""
    result = await _map(source, make_rss_entry(published=None))
    assert result.published_at is None


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.asyncio
async def test_mapping_passes_published_value_through_unchanged(source: type) -> None:
    """Reader が出した published を写像は補正せずそのまま渡す。"""
    result = await _map(source, make_rss_entry(published=_PUBLISHED))
    assert result.published_at == _PUBLISHED


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.asyncio
async def test_url_comes_from_link_not_guid(source: type) -> None:
    """URL 形の guid と衝突させても url は link を出所とする。"""
    entry = make_rss_entry(
        link="https://example.com/real-article",
        guid="https://example.com/wrong-guid-as-url",
    )
    result = await _map(source, entry)
    assert result.url == "https://example.com/real-article"
    assert result.url != entry.guid


@pytest.mark.asyncio
async def test_the_register_expands_redirector_link_to_real_host() -> None:
    """redirector URL は実 host へ展開する (純粋 URL 組立、canonicalize ではない)。"""
    entry = make_rss_entry(
        link="https://go.theregister.com/feed/www.theregister.com/2026/05/01/foo/"
    )
    result = await _map(TheRegisterSource, entry)
    assert result.url == "https://www.theregister.com/2026/05/01/foo/"


@pytest.mark.asyncio
async def test_the_register_passes_non_redirector_link_through_unchanged() -> None:
    """redirector prefix を持たない link は変換せず素通す。"""
    entry = make_rss_entry(link="https://www.theregister.com/2026/05/01/direct/")
    result = await _map(TheRegisterSource, entry)
    assert result.url == "https://www.theregister.com/2026/05/01/direct/"


@pytest.mark.asyncio
async def test_the_register_maps_body_to_none_even_when_summary_present() -> None:
    """The Register は summary が在っても body を採らない。"""
    entry = make_rss_entry(summary="<p>" + "x" * 500 + "</p>", content_encoded=None)
    assert (await _map(TheRegisterSource, entry)).body is None


@pytest.mark.asyncio
async def test_the_register_does_not_drop_empty_link() -> None:
    """空 link でも写像は drop / raise せず空 url のまま素通す。

    空 link の棄却 (acquisition_conversion_url_missing 可視化) は converter の
    責務であり、写像が握り潰すと故障が監査されないため写像は裁かない。
    """
    result = await _map(TheRegisterSource, make_rss_entry(link=""))
    assert isinstance(result, FetchedArticle)
    assert result.url == ""


def test_esa_djangoplicity_passes_empty_title_through() -> None:
    """空 title でも drop / raise せず空 str のまま素通す。

    空 title の棄却 (MISSING_TITLE 可視化) は converter の責務であり、
    写像が握り潰すと故障が監査されないため写像は裁かない。
    """
    result = esa_to_fetched_article(make_rss_entry(title=""))
    assert isinstance(result, FetchedArticle)
    assert result.title == ""


def test_esa_djangoplicity_does_not_truncate_long_title() -> None:
    """500 字 cap は converter 一元、写像は per-source 複製しない。"""
    long_title = "x" * 1000
    result = esa_to_fetched_article(make_rss_entry(title=long_title))
    assert len(result.title) == 1000


def test_esa_djangoplicity_maps_body_to_none_regardless_of_feed_body() -> None:
    """Pattern H: body は HTML 詳細補完のため常に None (RSS の本文は無視)。"""
    entry = make_rss_entry(
        content_encoded="<p>" + "x" * 500 + "</p>", summary="<p>" + "y" * 500 + "</p>"
    )
    assert esa_to_fetched_article(entry).body is None


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.asyncio
async def test_mapping_is_pure_same_entry_same_result(source: type) -> None:
    """同じ Entry を 2 回 → byte 等価 (IO / 状態を持たない pure 写像)。"""
    entry = make_rss_entry()
    assert (await _map(source, entry)) == (await _map(source, entry))


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.asyncio
async def test_mapping_is_total_on_degenerate_entry(source: type) -> None:
    """全欠落 Entry でも raise せず FetchedArticle を返す (total)。"""
    entry = make_rss_entry(
        title="", link="", summary=None, content_encoded=None, published=None, guid=None
    )
    assert isinstance((await _map(source, entry)), FetchedArticle)
