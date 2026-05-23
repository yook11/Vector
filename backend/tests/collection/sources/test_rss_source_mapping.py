"""Source mapping (``RssEntry`` → ``FetchedArticle``) の新契約テスト。

HTTP / fixture / DB 非依存。``XxxSource.to_fetched_article`` (classmethod) と
ESA Djangoplicity family の module-level ``esa.to_fetched_article`` を
手製 ``RssEntry`` で直接叩き、写像が宣言通り写すこと、および写像が **裁かない**
(品質ゲート / URL 検証 / drop を converter に委ね、生値を素通しする) ことを
固定する。enumerable な body policy が 2 source 以上で共有されるまで policy
表テストは作らず、TechCrunch / VentureBeat / The Register の代表 3 写像で契約を
釘打つ。The Register は source 固有 URL 変換 (redirector→実 host) が
canonicalize/SSRF でなく純粋 URL 組立に留まること、および空 link を写像で
drop せず素通すこと (棄却の可視化は converter) を担う。ESA Djangoplicity は
Pattern H (body は HTML 詳細補完のため None 固定) を写像で宣言する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.reader.rss_reader import RssEntry
from app.collection.domain.article_limits import ARTICLE_BODY_MAX_LENGTH
from app.collection.sources.definitions.esa import (
    to_fetched_article as esa_to_fetched_article,
)
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from app.collection.sources.definitions.the_register import TheRegisterSource
from app.collection.sources.definitions.venturebeat import VentureBeatSource

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

# to_fetched_article を持つ代表 source。共通の不変条件を parametrize。
# The Register の redirector 変換は SSRF/javascript テスト URL の prefix に
# 一致しないため raw URL 素通し不変条件を侵さない (共通 set に同居可)。
_SOURCES = [TechCrunchSource, VentureBeatSource, TheRegisterSource]


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


# --- 正の写像: 宣言通り写すか ---------------------------------------------


def test_techcrunch_maps_body_to_none_even_when_feed_carries_content() -> None:
    """TC は content_encoded / summary が在っても body を採らない。"""
    entry = make_rss_entry(
        content_encoded="<p>" + "x" * 500 + "</p>", summary="<p>" + "y" * 500 + "</p>"
    )
    assert TechCrunchSource.to_fetched_article(entry).body is None


def test_techcrunch_passes_through_title_url_published() -> None:
    entry = make_rss_entry()
    result = TechCrunchSource.to_fetched_article(entry)
    assert (result.title, result.url, result.published_at) == (
        entry.title,
        entry.link,
        entry.published,
    )


def test_venturebeat_picks_longer_content_encoded_as_body() -> None:
    entry = make_rss_entry(
        content_encoded="<p>" + "A" * 100 + "</p>", summary="<p>short</p>"
    )
    assert VentureBeatSource.to_fetched_article(entry).body == "A" * 100


def test_venturebeat_picks_longer_summary_as_body() -> None:
    entry = make_rss_entry(
        content_encoded="<p>short</p>", summary="<p>" + "B" * 100 + "</p>"
    )
    assert VentureBeatSource.to_fetched_article(entry).body == "B" * 100


def test_venturebeat_strips_html_tags_and_decodes_entities_in_body() -> None:
    entry = make_rss_entry(
        content_encoded="<p>Hello &amp; <b>world</b></p>", summary=""
    )
    body = VentureBeatSource.to_fetched_article(entry).body
    assert body == "Hello & world"


# --- 負の不変条件: 写像は裁かない ------------------------------------------


def test_venturebeat_returns_none_body_when_both_sources_empty() -> None:
    """body 候補が無くても None を返すだけで raise / drop しない。"""
    entry = make_rss_entry(content_encoded=None, summary=None)
    result = VentureBeatSource.to_fetched_article(entry)
    assert isinstance(result, FetchedArticle)
    assert result.body is None


@pytest.mark.parametrize("source", _SOURCES)
def test_mapping_does_not_reject_empty_title(source: type) -> None:
    """空 title は写像で裁かず空 str のまま FetchedArticle に載る。"""
    result = source.to_fetched_article(make_rss_entry(title=""))
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
def test_mapping_passes_raw_url_through_without_canonicalize_or_validation(
    source: type, raw_url: str
) -> None:
    """canonicalize / SSRF 検証は converter の責務。写像は生 URL を素通し。"""
    result = source.to_fetched_article(make_rss_entry(link=raw_url))
    assert result.url == raw_url


def test_venturebeat_passes_short_body_through_without_min_length_judgement() -> None:
    """短いが非空の body は短さで None 化しない (空→None は strip 結果と別)。"""
    entry = make_rss_entry(content_encoded="x", summary=None)
    assert VentureBeatSource.to_fetched_article(entry).body == "x"


def test_venturebeat_passes_oversized_body_through_without_truncation() -> None:
    """converter の最大長 cap を写像は適用しない (素通し)。"""
    huge = "y" * (ARTICLE_BODY_MAX_LENGTH + 1)
    entry = make_rss_entry(content_encoded="<p>" + huge + "</p>", summary=None)
    assert len(VentureBeatSource.to_fetched_article(entry).body or "") == len(huge)


@pytest.mark.parametrize("source", _SOURCES)
def test_mapping_passes_published_none_through(source: type) -> None:
    """published 不在は捏造せず None のまま素通し。"""
    result = source.to_fetched_article(make_rss_entry(published=None))
    assert result.published_at is None


@pytest.mark.parametrize("source", _SOURCES)
def test_mapping_passes_published_value_through_unchanged(source: type) -> None:
    """Reader が出した published を写像は補正せずそのまま渡す。"""
    result = source.to_fetched_article(make_rss_entry(published=_PUBLISHED))
    assert result.published_at == _PUBLISHED


# --- url provenance: link 由来であって guid 由来でない ----------------------


@pytest.mark.parametrize("source", _SOURCES)
def test_url_comes_from_link_not_guid(source: type) -> None:
    """URL 形の guid と衝突させても url は link を出所とする。"""
    entry = make_rss_entry(
        link="https://example.com/real-article",
        guid="https://example.com/wrong-guid-as-url",
    )
    result = source.to_fetched_article(entry)
    assert result.url == "https://example.com/real-article"
    assert result.url != entry.guid


# --- The Register: source 固有 URL 変換 + drop 除去 -------------------------


def test_the_register_expands_redirector_link_to_real_host() -> None:
    """redirector URL は実 host へ展開する (純粋 URL 組立、canonicalize ではない)。"""
    entry = make_rss_entry(
        link="https://go.theregister.com/feed/www.theregister.com/2026/05/01/foo/"
    )
    result = TheRegisterSource.to_fetched_article(entry)
    assert result.url == "https://www.theregister.com/2026/05/01/foo/"


def test_the_register_passes_non_redirector_link_through_unchanged() -> None:
    """redirector prefix を持たない link は変換せず素通す。"""
    entry = make_rss_entry(link="https://www.theregister.com/2026/05/01/direct/")
    result = TheRegisterSource.to_fetched_article(entry)
    assert result.url == "https://www.theregister.com/2026/05/01/direct/"


def test_the_register_maps_body_to_none_even_when_summary_present() -> None:
    """The Register は summary が在っても body を採らない。"""
    entry = make_rss_entry(summary="<p>" + "x" * 500 + "</p>", content_encoded=None)
    assert TheRegisterSource.to_fetched_article(entry).body is None


def test_the_register_does_not_drop_empty_link() -> None:
    """空 link でも写像は drop / raise せず空 url のまま素通す。

    空 link の棄却 (MISSING_URL 可視化) は converter の責務であり、
    写像が握り潰すと故障が監査されないため写像は裁かない。
    """
    result = TheRegisterSource.to_fetched_article(make_rss_entry(link=""))
    assert isinstance(result, FetchedArticle)
    assert result.url == ""


# --- ESA Djangoplicity (Hubble/Webb 共通): module-level 写像 seam ----------


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


# --- 構造: pure / total ----------------------------------------------------


@pytest.mark.parametrize("source", _SOURCES)
def test_mapping_is_pure_same_entry_same_result(source: type) -> None:
    """同じ Entry を 2 回 → byte 等価 (IO / 状態を持たない pure 写像)。"""
    entry = make_rss_entry()
    assert source.to_fetched_article(entry) == source.to_fetched_article(entry)


@pytest.mark.parametrize("source", _SOURCES)
def test_mapping_is_total_on_degenerate_entry(source: type) -> None:
    """全欠落 Entry でも raise せず FetchedArticle を返す (total)。"""
    entry = make_rss_entry(
        title="", link="", summary=None, content_encoded=None, published=None, guid=None
    )
    assert isinstance(source.to_fetched_article(entry), FetchedArticle)
