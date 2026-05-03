"""``HuggingFaceBlogFetcher`` の単体テスト (Phase 3 PR 3-d-2)。

per-source 設計:
- RSS 2.0、``<description>`` 空、本文は HTML 抽出に完全委譲 → Pattern H
- ``<author>`` 未提供 → ``metadata.author = "Hugging Face"`` hardcode
- ``<link>`` の ``/blog/<org>/<slug>`` から org 抽出 → ``extras = {"hf_org": ...}``
- PROVIDES = {language, guid, site_name, author}
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.huggingface import (
    HuggingFaceBlogFetcher,
    _extract_hf_org,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "huggingface_blog_rss.xml"
)

_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "HF Test Article",
        "link": "https://huggingface.co/blog/test-slug",
        "id": "https://huggingface.co/blog/test-slug",
        "published_parsed": time.struct_time((2026, 4, 29, 12, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestExtractHfOrg:
    def test_official_blog_returns_none(self) -> None:
        assert _extract_hf_org("https://huggingface.co/blog/foo-bar") is None

    def test_official_blog_with_trailing_slash_returns_none(self) -> None:
        assert _extract_hf_org("https://huggingface.co/blog/foo-bar/") is None

    def test_community_org_returns_org_name(self) -> None:
        assert (
            _extract_hf_org(
                "https://huggingface.co/blog/evaleval/eval-costs-bottleneck"
            )
            == "evaleval"
        )

    def test_community_with_trailing_slash_returns_org(self) -> None:
        assert (
            _extract_hf_org("https://huggingface.co/blog/ibm-granite/granite-4-1/")
            == "ibm-granite"
        )

    def test_empty_link_returns_none(self) -> None:
        assert _extract_hf_org("") is None

    def test_unrelated_path_returns_none(self) -> None:
        # /blog/ で始まらない path は除外
        assert _extract_hf_org("https://huggingface.co/datasets/some-dataset") is None

    def test_deeper_path_returns_none(self) -> None:
        # /blog/<org>/<slug>/<more> は対象外 (regex で末端を絞る)
        assert _extract_hf_org("https://huggingface.co/blog/foo/bar/extra") is None


class TestProvides:
    def test_provides_includes_author(self) -> None:
        assert HuggingFaceBlogFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name", "author"}
        )

    def test_endpoint_is_canonical(self) -> None:
        assert HuggingFaceBlogFetcher.ENDPOINT_URL == (
            "https://huggingface.co/blog/feed.xml"
        )


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = HuggingFaceBlogFetcher()

    def test_valid_official_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "HF Test Article"

    def test_official_entry_extras_is_none(self) -> None:
        # /blog/<slug> 単独形式は org なし → extras=None
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.extras is None

    def test_community_entry_extras_includes_hf_org(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(
                link="https://huggingface.co/blog/evaleval/eval-costs-bottleneck",
                id="https://huggingface.co/blog/evaleval/eval-costs-bottleneck",
            ),
            _SOURCE_ID,
            "en-US",
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.extras == {"hf_org": "evaleval"}

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), _SOURCE_ID, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_metadata_author_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "Hugging Face"

    def test_metadata_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "Hugging Face"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3

    def test_fixture_first_entry_extracts_evaleval_org(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = HuggingFaceBlogFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.extras == {"hf_org": "evaleval"}

    def test_fixture_second_entry_extracts_ibm_granite_org(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = HuggingFaceBlogFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.extras == {"hf_org": "ibm-granite"}

    def test_fixture_third_entry_official_no_org(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = HuggingFaceBlogFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        # /blog/inference-providers-deepinfra is official
        assert outcome.metadata.extras is None
