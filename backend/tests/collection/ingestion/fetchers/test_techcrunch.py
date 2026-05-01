"""``TechCrunchFetcher`` の単体テスト (collection-acquisition-redesign Phase 1b')。

``_convert_entry`` を純関数として直接テストし、feed XML 全体は
``feedparser.parse`` 経由で hand-crafted fixture からのサニティ確認を行う。
``fetch`` の HTTP 取得部分 (``_fetch_feed``) は make_safe_async_client / SSRF
guard と密結合でテスト価値が低いため、本ファイルでは触れない (結合テストは
``test_extract_html_body.py`` で extract 段の結合を扱う)。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.techcrunch import (
    TechCrunchFetcher,
    _normalize_language,
    _strip_html,
)
from app.models.news_source import NewsSource

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "techcrunch_rss.xml"
)


def _source(source_id: int = 1, name: str = "TechCrunch") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://techcrunch.com/feed/"
    return s


def _entry(**overrides: Any) -> dict[str, Any]:
    """feedparser-like dict を作る helper。"""
    base: dict[str, Any] = {
        "title": "Test TC Title",
        "link": "https://techcrunch.com/article/",
        "id": "https://techcrunch.com/?p=1",
        "summary": "<p>Lead paragraph only, ~140 chars in TC RSS.</p>",
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "author": "Jane Doe",
        "tags": [{"term": "AI"}, {"term": "Funding"}],
        "media_content": [
            {"url": "https://techcrunch.com/cover.jpg", "medium": "image"}
        ],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_is_frozenset_of_str(self) -> None:
        assert isinstance(TechCrunchFetcher.PROVIDES, frozenset)
        assert all(isinstance(f, str) for f in TechCrunchFetcher.PROVIDES)

    def test_provides_minimum_set(self) -> None:
        # 100% 提供保証: language (feed-level) / guid (RSS 必須) / site_name (hardcode)
        # author / tags / image_url は probabilistic なので含まない
        assert TechCrunchFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestStripHtml:
    def test_removes_tags(self) -> None:
        assert _strip_html("<p>hello <b>world</b></p>") == "hello world"

    def test_unescapes_entities(self) -> None:
        assert _strip_html("AT&amp;T &lt;br&gt;") == "AT&T <br>"

    def test_collapses_whitespace(self) -> None:
        assert _strip_html("a   b\n\nc") == "a b c"

    def test_empty_returns_empty(self) -> None:
        assert _strip_html("") == ""


class TestNormalizeLanguage:
    def test_default_when_missing(self) -> None:
        assert _normalize_language(None) == "en-US"

    def test_underscore_to_hyphen(self) -> None:
        assert _normalize_language("en_US") == "en-US"

    def test_passthrough(self) -> None:
        assert _normalize_language("ja-JP") == "ja-JP"

    def test_truncation(self) -> None:
        assert len(_normalize_language("x" * 100)) == 20


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = TechCrunchFetcher()
        self.source = _source()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "Test TC Title"
        assert outcome.source_id == 1
        assert str(outcome.source_url) == "https://techcrunch.com/article/"
        assert outcome.published_at_hint is not None
        assert outcome.published_at_hint.value.year == 2026

    def test_does_not_construct_body(self) -> None:
        """Pattern H Fetcher は本文を作らない (body は HTML 側で抽出する責務)。"""
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        # PendingHtmlFetch には body field が存在しない (型レベルの保証)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_missing_published_yields_pending_with_none(self) -> None:
        """Pattern H 固有: pubDate 欠落でも Failed しない (HTML が補完しうる)。"""
        e = _entry()
        del e["published_parsed"]
        outcome = self.fetcher._convert_entry(e, self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ("AI", "Funding")

    def test_extracts_image_url(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is not None
        assert str(outcome.metadata.image_url) == "https://techcrunch.com/cover.jpg"

    def test_extracts_author(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "Jane Doe"

    def test_extracts_guid(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == "https://techcrunch.com/?p=1"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "TechCrunch"

    def test_falls_back_to_updated_parsed(self) -> None:
        e = _entry()
        e["updated_parsed"] = e.pop("published_parsed")
        outcome = self.fetcher._convert_entry(e, self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None

    def test_published_at_hint_is_utc(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert outcome.published_at_hint.value.tzinfo is not None


class TestFixtureParsing:
    """hand-crafted fixture (TC RSS の最小サンプル) を feedparser に通せること。"""

    def test_fixture_parseable(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        assert not feed.bozo or feed.entries
        assert len(feed.entries) == 3
        assert feed.feed.get("language") == "en-US"

    def test_fixture_first_entry_yields_pending(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = TechCrunchFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("OpenAI raises")
        assert outcome.metadata.language == "en-US"
        assert outcome.metadata.site_name == "TechCrunch"
        assert outcome.metadata.author == "Alex Wilhelm"
        assert "AI" in outcome.metadata.tags
        assert "Funding" in outcome.metadata.tags

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = TechCrunchFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _source(), "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
