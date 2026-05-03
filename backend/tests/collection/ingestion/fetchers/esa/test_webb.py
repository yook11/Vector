"""``ESAWebbFetcher`` の ClassVar 整合性 + fixture 解析テスト (PR 3-b)。"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.ingestion.domain.fetched_article import PendingHtmlFetch
from app.collection.ingestion.fetchers.esa.webb import ESAWebbFetcher

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent.parent / "fixtures" / "esa_webb_rss.xml"
)

_SOURCE_ID = 1


class TestClassVars:
    def test_name_is_esa_webb(self) -> None:
        assert ESAWebbFetcher.NAME == "ESA/Webb"

    def test_endpoint_is_canonical_feed(self) -> None:
        assert ESAWebbFetcher.ENDPOINT_URL == "https://esawebb.org/news/feed/"

    def test_site_name_matches_name(self) -> None:
        assert ESAWebbFetcher.SITE_NAME == "ESA/Webb"

    def test_author_is_esa_webb(self) -> None:
        assert ESAWebbFetcher.AUTHOR == "ESA/Webb"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3

    def test_fixture_first_entry_is_pattern_h_pending(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ESAWebbFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("Science Release: Webb")
        assert outcome.metadata.author == "ESA/Webb"
        assert outcome.metadata.site_name == "ESA/Webb"
        assert outcome.metadata.language == "en"

    def test_fixture_first_entry_link_is_djangoplicity_url(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ESAWebbFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert str(outcome.source_url.root).startswith("https://esawebb.org/news/weic")

    def test_fixture_first_entry_guid_matches_link(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ESAWebbFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == str(outcome.source_url.root)

    def test_fixture_first_entry_published_hint_is_utc(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ESAWebbFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        offset = outcome.published_at_hint.value.tzinfo.utcoffset(None)
        assert offset is not None and offset.total_seconds() == 0
