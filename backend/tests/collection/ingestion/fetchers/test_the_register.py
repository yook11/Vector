"""``TheRegisterFetcher`` の単体テスト (Phase 1d、Pattern R+H 最終)。

per-source 設計:
- Atom feed (RFC4287) を feedparser 標準経路で解釈
- ``<link href>`` は redirector URL → ``_normalize_register_link`` で実 URL
- ``<id>`` は ``tag:theregister.com,2005:story...`` URI 形式 (NOT redirector)
  → そのまま guid 採用
- ``<author><name>`` を author に採用 (email/uri は捨てる)
- ``<category>`` 未提供のため tags=() 直書き
- ``<media:>`` namespace 未宣言 → image_url=None 直書き
- language は feed-level ``xml:lang="en"``
- PROVIDES = {language, guid, site_name}
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
from app.collection.ingestion.fetchers.the_register import (
    TheRegisterFetcher,
    _normalize_register_link,
)
from app.models.news_source import NewsSource

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "the_register_atom.xml"
)


def _source(source_id: int = 1, name: str = "The Register") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://www.theregister.com/headlines.atom"
    return s


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "ServiceNow under siege as Atlassian adds to ITSM take-outs",
        "link": "https://go.theregister.com/feed/www.theregister.com/2026/05/01/servicenow_under_siege/",
        "id": "tag:theregister.com,2005:story245938",
        "author": "O'Ryan Johnson",
        "published_parsed": time.struct_time((2026, 5, 1, 21, 39, 10, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestNormalizeRegisterLink:
    """純関数 ``_normalize_register_link`` の挙動。"""

    def test_redirector_link_is_unwrapped(self) -> None:
        result = _normalize_register_link(
            "https://go.theregister.com/feed/www.theregister.com/2026/05/01/servicenow/"
        )
        assert result == "https://www.theregister.com/2026/05/01/servicenow/"

    def test_direct_link_is_passthrough(self) -> None:
        result = _normalize_register_link(
            "https://www.theregister.com/2026/05/01/direct/"
        )
        assert result == "https://www.theregister.com/2026/05/01/direct/"

    def test_empty_string_is_passthrough(self) -> None:
        assert _normalize_register_link("") == ""

    def test_unrelated_link_is_passthrough(self) -> None:
        assert (
            _normalize_register_link("https://example.com/foo")
            == "https://example.com/foo"
        )


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert TheRegisterFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = TheRegisterFetcher()
        self.source = _source()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("ServiceNow")

    def test_redirector_link_normalized_in_source_url(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        url = str(outcome.source_url)
        assert "go.theregister.com" not in url
        assert url.startswith("https://www.theregister.com/")

    def test_direct_link_passthrough(self) -> None:
        entry = _entry(link="https://www.theregister.com/2026/05/01/direct/")
        outcome = self.fetcher._convert_entry(entry, self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert str(outcome.source_url) == (
            "https://www.theregister.com/2026/05/01/direct/"
        )

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_empty_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(link=""), self.source, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_published_parsed_yields_utc(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert (
            outcome.published_at_hint.value.tzinfo.utcoffset(None).total_seconds() == 0
        )

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_from_atom_author_name(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "O'Ryan Johnson"

    def test_metadata_tags_hardcoded_empty(self) -> None:
        # The Register Atom は <category> を提供しない
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ()

    def test_metadata_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_atom_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        # tag: URI scheme は redirector ではなく直接 guid 採用
        assert outcome.metadata.guid == "tag:theregister.com,2005:story245938"

    def test_language_passthrough_en(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        # NOT en-US (xml:lang="en" 仕様)
        assert outcome.metadata.language == "en"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "The Register"


class TestFixtureParsing:
    def test_fixture_parses_as_atom(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        assert len(feed.entries) == 3

    def test_fixture_first_entry_yields_pending_with_normalized_url(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = TheRegisterFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "en")
        assert isinstance(outcome, PendingHtmlFetch)
        url = str(outcome.source_url)
        assert "go.theregister.com" not in url
        assert url.startswith("https://www.theregister.com/")
        assert outcome.metadata.language == "en"
        assert outcome.metadata.site_name == "The Register"
        assert outcome.metadata.guid is not None
        assert outcome.metadata.guid.startswith("tag:theregister.com")

    def test_fixture_second_entry_direct_url_passthrough(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = TheRegisterFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _source(), "en")
        assert isinstance(outcome, PendingHtmlFetch)
        # 2 entry目は直接 URL (redirector 無し)
        assert str(outcome.source_url).startswith("https://www.theregister.com/")

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = TheRegisterFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _source(), "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
