"""``BaseFrontiersFetcher`` の helper / convert_entry 単体テスト (Phase 3 PR 3-c-3)。

per-source 設計:
- RSS 2.0 (UTF-8)、Pattern R via ``<description>`` (abstract 全文)
- ``<author>`` 単一 (corresponding author)
- ``<category>`` 記事種別 (Original Research 等) → tags に詰めない (空 tuple)
- license CC BY 4.0 hardcode、DOI を link から正規表現抽出
- PROVIDES = {language, guid, site_name, author}
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
from app.collection.ingestion.fetchers.frontiers._common import (
    BaseFrontiersFetcher,
    _extract_doi,
)

_FIXTURES = Path(__file__).parent.parent.parent.parent.parent / "fixtures"
_FIXTURE_AI = _FIXTURES / "frontiers_ai_rss.xml"

_SOURCE_ID = 1


class _ConcreteFrontiersFetcher(BaseFrontiersFetcher):
    """テスト専用の concrete subclass (ClassVar 必須項目を埋める)。"""

    NAME = "Frontiers in Test Journal"
    ENDPOINT_URL = "https://www.frontiersin.org/journals/test-journal/rss"
    JOURNAL_NAME = "Frontiers in Test Journal"


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "A study on neural architecture search for time series",
        "link": "https://www.frontiersin.org/articles/10.3389/frai.2026.1234567",
        "id": "https://www.frontiersin.org/articles/10.3389/frai.2026.1234567",
        "summary": (
            "This is a long enough abstract describing our novel approach "
            "to neural architecture search for time series forecasting. "
            "We propose a method that achieves state-of-the-art performance "
            "across multiple benchmark datasets. The proposed approach is "
            "computationally efficient and theoretically grounded."
        ),
        "published_parsed": time.struct_time((2026, 5, 4, 0, 0, 0, 0, 0, 0)),
        "author": "Jane Researcher",
    }
    base.update(overrides)
    return base


class TestExtractDOI:
    def test_extracts_frai_doi_from_link(self) -> None:
        link = "https://www.frontiersin.org/articles/10.3389/frai.2026.1767330"
        assert _extract_doi(link) == "10.3389/frai.2026.1767330"

    def test_extracts_frobt_doi(self) -> None:
        link = "https://www.frontiersin.org/articles/10.3389/frobt.2026.1767798"
        assert _extract_doi(link) == "10.3389/frobt.2026.1767798"

    def test_extracts_fenrg_doi(self) -> None:
        link = "https://www.frontiersin.org/articles/10.3389/fenrg.2026.1718662"
        assert _extract_doi(link) == "10.3389/fenrg.2026.1718662"

    def test_extracts_fmats_doi(self) -> None:
        link = "https://www.frontiersin.org/articles/10.3389/fmats.2026.1802326"
        assert _extract_doi(link) == "10.3389/fmats.2026.1802326"

    def test_returns_none_for_non_doi_url(self) -> None:
        assert _extract_doi("https://example.com/no-doi") is None

    def test_returns_none_for_none_input(self) -> None:
        assert _extract_doi(None) is None


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = _ConcreteFrontiersFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_ready(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("A study on neural")
        assert outcome.article.body.startswith("This is a long enough")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        # Editorial / Correction で description が空のケース
        outcome = self.fetcher._convert_entry(
            _entry(summary="brief."), self.source_id, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_missing_pubdate_returns_failed(self) -> None:
        # Pattern R は published_at 必須 (Failed で drop)
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "published_at_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_metadata_author_passthrough(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author == "Jane Researcher"

    def test_metadata_author_none_when_missing(self) -> None:
        entry = _entry()
        del entry["author"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author is None

    def test_metadata_tags_empty_ignoring_article_type(self) -> None:
        # <category>Original Research</category> は記事種別なので tags に
        # 詰めない (空 tuple のまま)。feedparser は category を tags にマップ
        # するが、_convert_entry は明示的に () で上書きする。
        entry = _entry(tags=[{"term": "Original Research"}])
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ()

    def test_metadata_extras_license_hardcode(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.extras is not None
        assert outcome.metadata.extras["license"] == "CC BY 4.0"

    def test_metadata_extras_doi_extracted(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.extras is not None
        assert outcome.metadata.extras["doi"] == "10.3389/frai.2026.1234567"

    def test_metadata_site_name_uses_journal_name(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.site_name == "Frontiers in Test Journal"

    def test_metadata_image_url_none(self) -> None:
        # Frontiers RSS は画像を提供しない
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is None

    def test_metadata_language_passthrough(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.language == "en"

    def test_metadata_guid_extracted(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert (
            outcome.metadata.guid
            == "https://www.frontiersin.org/articles/10.3389/frai.2026.1234567"
        )


class TestFixtureParsing:
    def test_fixture_parses_two_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE_AI.read_bytes())
        assert len(feed.entries) == 2

    def test_fixture_first_entry_yields_ready(self) -> None:
        feed = feedparser.parse(_FIXTURE_AI.read_bytes())
        fetcher = _ConcreteFrontiersFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, ReadyForArticle)
        assert "HyRA-CXR" in outcome.article.title
        assert "hybrid architecture" in outcome.article.body
        assert outcome.metadata.author == "Wadhah Zeyad Tareq"
        assert outcome.metadata.extras is not None
        assert outcome.metadata.extras["doi"] == "10.3389/frai.2026.1767330"

    def test_fixture_second_entry_drops_short_editorial(self) -> None:
        feed = feedparser.parse(_FIXTURE_AI.read_bytes())
        fetcher = _ConcreteFrontiersFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID, "en")
        # Editorial (description "Brief editorial." 16 chars) は body_too_short で drop
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"
