"""``PLOSOneFetcher`` の単体テスト (Phase 3 PR 3-c-1)。

per-source 設計:
- Atom 1.0、``<content type="html">`` に abstract (1.4K-3K chars)
- ``<id>`` は DOI 文字列 (URL ではない)
- 多重 ``<author><name>`` → ``authors`` tuple、重複除去
- ``<rights>`` は提供されないため license は CC BY 4.0 hardcode
- editorial note は body_too_short で drop
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
from app.collection.ingestion.fetchers.plos_one import (
    PLOSOneFetcher,
    _extract_authors,
    _extract_doi,
    _pick_body,
)

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "plos_one_atom.xml"

_SOURCE_ID = 1


_ABSTRACT = (
    "Rho of Plants (ROPs) are plant-specific Rho GTPases that regulate diverse "
    "cellular processes. " * 5
)


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "PLOS Test Article",
        "link": "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0348001",
        "id": "10.1371/journal.pone.0348001",
        "content": [{"value": "<p>" + _ABSTRACT + "</p>"}],
        "summary": "short summary",
        "published_parsed": time.struct_time((2026, 5, 1, 14, 0, 0, 0, 0, 0)),
        "authors": [
            {"name": "Alice"},
            {"name": "Bob"},
            {"name": "Alice"},  # 重複
        ],
    }
    base.update(overrides)
    return base


class TestExtractAuthors:
    def test_dedup_preserves_order(self) -> None:
        entry = {"authors": [{"name": "Alice"}, {"name": "Bob"}, {"name": "Alice"}]}
        assert _extract_authors(entry) == ("Alice", "Bob")

    def test_no_authors_returns_empty(self) -> None:
        assert _extract_authors({}) == ()


class TestExtractDoi:
    def test_pone_doi(self) -> None:
        doi = "10.1371/journal.pone.0348001"
        assert _extract_doi(doi) == doi

    def test_pbio_doi(self) -> None:
        # 他 PLOS journal も同 prefix family、本 fetcher は pone のみ対象だが
        # 正規表現は journal slug を 1 char+ で許容するため pbio もマッチする
        assert _extract_doi("10.1371/journal.pbio.0001") == "10.1371/journal.pbio.0001"

    def test_url_returns_none(self) -> None:
        # PLOS の id は DOI 文字列のみ。eLife のような URL は対象外
        assert _extract_doi("https://dx.doi.org/10.1371/journal.pone.001") is None

    def test_empty(self) -> None:
        assert _extract_doi(None) is None
        assert _extract_doi("") is None


class TestPickBody:
    def test_atom_content(self) -> None:
        assert _pick_body({"content": [{"value": "abstract"}]}) == "abstract"

    def test_falls_back_to_summary(self) -> None:
        assert _pick_body({"summary": "short"}) == "short"

    def test_empty_when_neither(self) -> None:
        assert _pick_body({}) == ""


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert PLOSOneFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})

    def test_endpoint_is_atom(self) -> None:
        assert PLOSOneFetcher.ENDPOINT_URL == (
            "https://journals.plos.org/plosone/feed/atom"
        )


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = PLOSOneFetcher()

    def test_valid_entry_yields_ready(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title == "PLOS Test Article"
        assert "Rho of Plants" in outcome.article.body

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), _SOURCE_ID, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(content=[{"value": "<p>too short</p>"}]),
            _SOURCE_ID,
            "en",
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_editorial_note_drops_via_body_gate(self) -> None:
        # 実 PLOS の editorial note は ~30 chars、本 gate で構造的に drop
        e = _entry(content=[{"value": "<p>by The PLOS One Editors </p>"}])
        outcome = self.fetcher._convert_entry(e, _SOURCE_ID, "en")
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

    def test_authors_tuple_dedup(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == ("Alice", "Bob")

    def test_primary_author_is_first(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author == "Alice"

    def test_guid_is_doi_string(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.guid == "10.1371/journal.pone.0348001"

    def test_extras_license_and_doi(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.extras is not None
        assert outcome.metadata.extras["license"] == "CC BY 4.0"
        assert outcome.metadata.extras["doi"] == "10.1371/journal.pone.0348001"

    def test_extras_license_only_when_id_not_doi(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(id="not-a-doi"), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.extras == {"license": "CC BY 4.0"}

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.site_name == "PLOS ONE"

    def test_language_is_en(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.language == "en"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3
        assert feed.version == "atom10"

    def test_fixture_first_entry_yields_ready(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = PLOSOneFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("C-terminal S-acylation")
        assert "ROP" in outcome.article.body
        assert outcome.metadata.authors == (
            "Amir Akerman",
            "Orit Gutman",
            "Keren E. Shapira",
            "Shaul Yalovsky",
        )
        assert outcome.metadata.extras is not None
        assert outcome.metadata.extras["doi"] == "10.1371/journal.pone.0348001"

    def test_fixture_third_entry_editorial_drops(self) -> None:
        # editorial note は body_too_short で構造的に drop される
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = PLOSOneFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"
