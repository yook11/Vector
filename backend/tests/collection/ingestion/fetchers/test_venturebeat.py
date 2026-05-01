"""``VentureBeatFetcher`` の単体テスト (collection-acquisition-redesign Phase 1a')。

``_convert_entry`` を純関数として直接テストし、feed XML 全体は
``feedparser.parse`` 経由で 1 件のサニティ確認を行う。``fetch`` の HTTP 取得
部分 (``_fetch_feed``) は make_safe_async_client / SSRF guard と密結合で
テスト価値が低いため、本ファイルでは触れない (結合テストは
``test_ingestion_service.py`` で扱う)。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.venturebeat import (
    VentureBeatFetcher,
    _normalize_language,
    _pick_body,
    _strip_html,
)
from app.models.news_source import NewsSource

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "venturebeat_rss.xml"
)


def _source(source_id: int = 1, name: str = "VentureBeat") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://venturebeat.com/feed/"
    return s


_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
_PERSPI = "Sed ut perspiciatis unde omnis iste natus error. "


def _entry(**overrides: Any) -> dict[str, Any]:
    """feedparser-like dict を作る helper。"""
    base: dict[str, Any] = {
        "title": "Test Title",
        "link": "https://venturebeat.com/article/",
        "id": "https://venturebeat.com/?p=1",
        "summary": "<p>" + _LOREM * 5 + "</p>",
        "content": [{"value": "<p>" + _PERSPI * 10 + "</p>"}],
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "author": "Jane Doe",
        "tags": [{"term": "AI"}, {"term": "Funding"}],
        "media_content": [{"url": "https://example.com/cover.jpg", "medium": "image"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_is_frozenset_of_str(self) -> None:
        assert isinstance(VentureBeatFetcher.PROVIDES, frozenset)
        assert all(isinstance(f, str) for f in VentureBeatFetcher.PROVIDES)

    def test_provides_minimum_set(self) -> None:
        # 実 feed で 100% 提供される項目のみ。Phase 1a' 実装時の確定値。
        assert VentureBeatFetcher.PROVIDES == frozenset(
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


class TestPickBody:
    def test_prefers_longer_content_encoded(self) -> None:
        entry = {
            "summary": "short",
            "content": [{"value": "x" * 1000}],
        }
        assert _pick_body(entry) == "x" * 1000

    def test_prefers_longer_summary(self) -> None:
        entry = {
            "summary": "y" * 1000,
            "content": [{"value": "short"}],
        }
        assert _pick_body(entry) == "y" * 1000

    def test_missing_content_returns_summary(self) -> None:
        assert _pick_body({"summary": "abc"}) == "abc"

    def test_missing_both_returns_empty(self) -> None:
        assert _pick_body({}) == ""


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
        self.fetcher = VentureBeatFetcher()
        self.source = _source()

    def test_valid_entry_yields_ready(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title == "Test Title"
        assert "Sed ut perspiciatis" in outcome.article.body
        assert outcome.metadata.language == "en-US"
        assert outcome.metadata.site_name == "VentureBeat"
        assert outcome.metadata.guid == "https://venturebeat.com/?p=1"

    def test_picks_longer_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        # content[0].value > summary なので content の中身が採用される
        assert "Sed ut perspiciatis" in outcome.article.body
        assert "Lorem ipsum" not in outcome.article.body

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(summary="<p>too short</p>", content=[{"value": "<p>tiny</p>"}]),
            self.source,
            "en-US",
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_missing_published_returns_failed(self) -> None:
        e = _entry()
        del e["published_parsed"]
        outcome = self.fetcher._convert_entry(e, self.source, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "published_at_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("AI", "Funding")

    def test_extracts_image_url(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is not None
        assert str(outcome.metadata.image_url) == "https://example.com/cover.jpg"

    def test_extracts_author(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author == "Jane Doe"

    def test_falls_back_to_updated_parsed(self) -> None:
        e = _entry()
        e["updated_parsed"] = e.pop("published_parsed")
        outcome = self.fetcher._convert_entry(e, self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)

    def test_published_at_is_utc(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.published_at.value.tzinfo is not None
        assert outcome.article.published_at.value.year == 2026


class TestFixtureParsing:
    """実 feed の最小サンプル (handcrafted XML) を feedparser に通せることを確認。"""

    def test_fixture_parseable(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        assert not feed.bozo or feed.entries  # parse 成功 or 部分成功
        assert len(feed.entries) == 2
        assert feed.feed.get("language") == "en-US"

    def test_fixture_first_entry_yields_ready(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = VentureBeatFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("AI startup raises")
        assert "$50M Series B" in outcome.article.body
        assert outcome.metadata.language == "en-US"
        assert outcome.metadata.site_name == "VentureBeat"
        assert outcome.metadata.author == "Jane Doe"
        assert "AI" in outcome.metadata.tags

    def test_fixture_second_entry_too_short_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = VentureBeatFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _source(), "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"
