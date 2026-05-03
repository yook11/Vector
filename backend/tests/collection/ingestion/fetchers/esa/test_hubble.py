"""``ESAHubbleFetcher`` の ClassVar 整合性 + fixture 解析テスト (PR 3-b)。

base 振る舞いは ``test__common.py`` で網羅、本ファイルは subclass 固有の
ClassVar 値と実 feed fixture でのパース確認に絞る。
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.ingestion.domain.fetched_article import PendingHtmlFetch
from app.collection.ingestion.fetchers.esa.hubble import ESAHubbleFetcher

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent.parent
    / "fixtures"
    / "esa_hubble_rss.xml"
)

_SOURCE_ID = 1


class TestClassVars:
    def test_name_is_esa_hubble(self) -> None:
        assert ESAHubbleFetcher.NAME == "ESA/Hubble"

    def test_endpoint_is_canonical_feed(self) -> None:
        assert ESAHubbleFetcher.ENDPOINT_URL == "https://esahubble.org/news/feed/"

    def test_site_name_matches_name(self) -> None:
        assert ESAHubbleFetcher.SITE_NAME == "ESA/Hubble"

    def test_author_is_esa_hubble(self) -> None:
        assert ESAHubbleFetcher.AUTHOR == "ESA/Hubble"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3

    def test_fixture_first_entry_is_pattern_h_pending(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ESAHubbleFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("Photo Release: Hubble")
        assert outcome.metadata.author == "ESA/Hubble"
        assert outcome.metadata.site_name == "ESA/Hubble"
        assert outcome.metadata.language == "en"

    def test_fixture_first_entry_link_is_djangoplicity_url(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ESAHubbleFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert str(outcome.source_url.root).startswith(
            "https://esahubble.org/news/heic"
        )

    def test_fixture_first_entry_guid_matches_link(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ESAHubbleFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == str(outcome.source_url.root)

    def test_fixture_first_entry_published_hint_is_utc(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ESAHubbleFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        # CET/CEST → UTC 正規化されている
        offset = outcome.published_at_hint.value.tzinfo.utcoffset(None)
        assert offset is not None and offset.total_seconds() == 0
