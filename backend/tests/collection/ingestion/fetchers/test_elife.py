"""``ELifeFetcher`` の単体テスト (Phase 3 PR 3-c-2)。

per-source 設計:
- RSS 2.0、本文は ``<description>`` (1.2-1.8K chars)、``<content:encoded>`` 空
- 多重 ``<author>`` (``"<email> (<name>)"``) → ``authors`` tuple、重複除去
- ``<webfeeds:featuredImage>`` → ``image_url`` 候補
- license CC BY 4.0 + DOI を ``extras`` に hardcode
- PROVIDES = {language, guid, site_name}
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.elife import (
    ELifeFetcher,
    _extract_authors,
    _extract_doi,
    _parse_author_name,
    _pick_body,
)

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "elife_rss.xml"

_SOURCE_ID = 1


_ABSTRACT = (
    "Circulating cell-free DNA (cfDNA) is valuable for molecular testing, "
    "but typically requires specialized collection tubes. " * 5
)


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "eLife Test Article",
        "link": "https://elifesciences.org/articles/123456",
        "id": "https://dx.doi.org/10.7554/eLife.123456",
        "summary": _ABSTRACT,
        "published_parsed": time.struct_time((2026, 5, 1, 0, 0, 0, 0, 0, 0)),
        "authors": [
            {"name": "Alice", "email": "x@y.com"},
            {"name": "Bob", "email": "x@y.com"},
        ],
        "author": "x@y.com (Bob)",
        "tags": [{"term": "Genetics and Genomics"}],
        "webfeeds_featuredimage": {
            "url": "https://elife-cdn.s3.amazonaws.com/observer/elife-logo.svg",
            "width": "408",
            "height": "230",
        },
    }
    base.update(overrides)
    return base


class TestParseAuthorName:
    def test_email_format(self) -> None:
        assert _parse_author_name("a@b.com (Alice)") == "Alice"

    def test_name_only(self) -> None:
        assert _parse_author_name("Alice") == "Alice"

    def test_empty(self) -> None:
        assert _parse_author_name("") is None


class TestExtractAuthors:
    def test_dedup_by_name(self) -> None:
        entry = {
            "authors": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Alice"},  # 重複
            ]
        }
        assert _extract_authors(entry) == ("Alice", "Bob")

    def test_strips_whitespace(self) -> None:
        entry = {"authors": [{"name": "  Alice  "}]}
        assert _extract_authors(entry) == ("Alice",)

    def test_skips_empty_name(self) -> None:
        entry = {"authors": [{"name": ""}, {"name": "Alice"}]}
        assert _extract_authors(entry) == ("Alice",)

    def test_skips_non_dict(self) -> None:
        entry = {"authors": ["Alice", {"name": "Bob"}]}
        assert _extract_authors(entry) == ("Bob",)

    def test_no_authors_returns_empty(self) -> None:
        assert _extract_authors({}) == ()


class TestExtractDoi:
    def test_dx_doi_url(self) -> None:
        assert (
            _extract_doi("https://dx.doi.org/10.7554/eLife.108439")
            == "10.7554/eLife.108439"
        )

    def test_doi_org_url(self) -> None:
        assert _extract_doi("https://doi.org/10.7554/eLife.999") == "10.7554/eLife.999"

    def test_non_doi_returns_none(self) -> None:
        assert _extract_doi("https://elifesciences.org/articles/108439") is None

    def test_empty(self) -> None:
        assert _extract_doi(None) is None
        assert _extract_doi("") is None


class TestPickBody:
    def test_summary_when_content_empty(self) -> None:
        assert _pick_body({"summary": "abstract here"}) == "abstract here"

    def test_picks_longer(self) -> None:
        entry = {"summary": "short", "content": [{"value": "x" * 100}]}
        assert _pick_body(entry) == "x" * 100


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert ELifeFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})

    def test_endpoint_is_recent_xml(self) -> None:
        assert ELifeFetcher.ENDPOINT_URL == "https://elifesciences.org/rss/recent.xml"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = ELifeFetcher()

    def test_valid_entry_yields_ready(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title == "eLife Test Article"
        assert "Circulating cell-free DNA" in outcome.article.body

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), _SOURCE_ID, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(summary="too short"), _SOURCE_ID, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_missing_published_returns_failed(self) -> None:
        e = _entry()
        del e["published_parsed"]
        outcome = self.fetcher._convert_entry(e, _SOURCE_ID, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "published_at_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), _SOURCE_ID, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_authors_tuple_extracted(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == ("Alice", "Bob")

    def test_primary_author_is_first_in_list(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author == "Alice"

    def test_primary_author_falls_back_to_author_field(self) -> None:
        e = _entry()
        e["authors"] = []
        outcome = self.fetcher._convert_entry(e, _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        # `x@y.com (Bob)` → "Bob"
        assert outcome.metadata.author == "Bob"

    def test_tags_extracted(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(tags=[{"term": "Cancer Biology"}, {"term": "Microbiology"}]),
            _SOURCE_ID,
            "en",
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Cancer Biology", "Microbiology")

    def test_image_url_from_webfeeds(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is not None
        assert "elife-cdn" in str(outcome.metadata.image_url)

    def test_image_url_none_when_missing(self) -> None:
        e = _entry()
        del e["webfeeds_featuredimage"]
        outcome = self.fetcher._convert_entry(e, _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is None

    def test_guid_is_doi_url(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.guid == "https://dx.doi.org/10.7554/eLife.123456"

    def test_extras_contains_license_and_doi(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.extras is not None
        assert outcome.metadata.extras["license"] == "CC BY 4.0"
        assert outcome.metadata.extras["doi"] == "10.7554/eLife.123456"

    def test_extras_license_only_when_no_doi(self) -> None:
        e = _entry(id="https://example.com/no-doi", link="https://example.com/no-doi")
        outcome = self.fetcher._convert_entry(e, _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.extras == {"license": "CC BY 4.0"}

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.site_name == "eLife"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3
        assert feed.feed.get("language") == "en"

    def test_fixture_first_entry_yields_ready(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ELifeFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert "Fusobacterium nucleatum" in outcome.article.title
        assert "NKp46" in outcome.article.body
        assert outcome.metadata.site_name == "eLife"
        assert outcome.metadata.extras is not None
        assert outcome.metadata.extras["doi"] == "10.7554/eLife.108439"

    def test_fixture_first_entry_has_dedup_authors(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ELifeFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        # 3 著者 (重複なし) が氏名のみで tuple 化
        assert outcome.metadata.authors == (
            "Ahmed Rishiq",
            "Gilad Bachrach",
            "Ofer Mandelboim",
        )

    def test_fixture_first_entry_image_extracted(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ELifeFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is not None
        assert "elife-cdn" in str(outcome.metadata.image_url)

    def test_fixture_third_entry_tags(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = ELifeFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Genetics and Genomics",)
