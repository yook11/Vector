"""``CornellChronicleFetcher`` の単体テスト (Phase 3 PR 3-e)。

per-source 設計:
- RSS 2.0 + dc namespace、Drupal 生成器
- ``<description>`` のみ (短い概要)、本文は HTML 抽出に委譲 → Pattern H
- ``<dc:creator>`` は Drupal 内部 ID → ``metadata.author = None`` で drop
- ``<image_featured>`` (Drupal 独自要素) を ``metadata.image_url`` に詰める
- ``<guid isPermaLink="true">`` は link と同値の絶対 URL
- 6 taxonomy term feed を ``FEEDS`` ClassVar で巡回 + ``seen_urls`` で dedup
- PROVIDES = {language, guid, site_name}
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import feedparser
import pytest

from app.collection.errors import TemporaryFetchError
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FetchOutcome,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.cornell import CornellChronicleFetcher

_FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"
_FIXTURE_AI = _FIXTURES / "cornell_rss.xml"
_FIXTURE_HEALTH = _FIXTURES / "cornell_rss_health.xml"

_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Cornell Test Article",
        "link": "https://news.cornell.edu/stories/2026/05/test-article",
        "id": "https://news.cornell.edu/stories/2026/05/test-article",
        "published_parsed": time.struct_time((2026, 5, 2, 14, 0, 0, 4, 122, 0)),
        "image_featured": "https://news.cornell.edu/sites/default/files/test.jpg",
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        # author は Drupal 内部 ID で drop するため PROVIDES に含めない。
        assert CornellChronicleFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )

    def test_endpoint_is_ai_feed(self) -> None:
        # 代表値として AI feed を採用。実 fetch は FEEDS の 6 URL 巡回。
        assert (
            CornellChronicleFetcher.ENDPOINT_URL
            == "https://news.cornell.edu/taxonomy/term/24043/feed"
        )

    def test_feeds_contains_six_taxonomy_terms(self) -> None:
        feeds = CornellChronicleFetcher.FEEDS
        assert len(feeds) == 6
        for url in feeds:
            assert url.startswith("https://news.cornell.edu/taxonomy/term/")
            assert url.endswith("/feed")

    def test_endpoint_url_is_in_feeds(self) -> None:
        # ENDPOINT_URL は news_sources.endpoint_url との互換だが、FEEDS に
        # 含まれていることで「fetch 対象から外れた」状態を防ぐ。
        assert CornellChronicleFetcher.ENDPOINT_URL in CornellChronicleFetcher.FEEDS


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = CornellChronicleFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "Cornell Test Article"

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H: 緩い品質ゲート、HTML 補完を待つ
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_is_none(self) -> None:
        # Drupal 内部 ID は人間名でないため drop。
        outcome = self.fetcher._convert_entry(
            _entry(author="kah53"), self.source_id, "en"
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author is None

    def test_metadata_image_url_from_image_featured(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is not None
        assert "test.jpg" in str(outcome.metadata.image_url.root)

    def test_image_url_falls_back_to_thumbnail_360(self) -> None:
        entry = _entry()
        del entry["image_featured"]
        entry["thumbnail_360x360"] = (
            "https://news.cornell.edu/sites/default/files/styles/thumb360.jpg"
        )
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is not None
        assert "thumb360" in str(outcome.metadata.image_url.root)

    def test_image_url_none_when_no_image_elements(self) -> None:
        entry = _entry()
        del entry["image_featured"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert (
            outcome.metadata.guid
            == "https://news.cornell.edu/stories/2026/05/test-article"
        )

    def test_language_passthrough(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "en"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "Cornell Chronicle"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE_AI.read_bytes())
        assert len(feed.entries) == 3

    def test_fixture_first_entry_is_pattern_h_pending(self) -> None:
        feed = feedparser.parse(_FIXTURE_AI.read_bytes())
        fetcher = CornellChronicleFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert "Frontiers of AI Summit" in outcome.title
        assert outcome.metadata.author is None
        assert outcome.metadata.site_name == "Cornell Chronicle"

    def test_fixture_first_entry_has_image_url(self) -> None:
        feed = feedparser.parse(_FIXTURE_AI.read_bytes())
        fetcher = CornellChronicleFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is not None
        assert "story_thumbnail_home" in str(outcome.metadata.image_url.root)

    def test_fixture_third_entry_pubdate_missing_yields_pending(self) -> None:
        # 3 件目は pubDate 欠落、Pattern H なので drop せず hint=None で通す
        feed = feedparser.parse(_FIXTURE_AI.read_bytes())
        fetcher = CornellChronicleFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None


class _StubFetcher(CornellChronicleFetcher):
    """``_fetch_feed`` を fixture 読込に差し替えた test double。

    複数 feed 巡回 (cross-feed dedup) と部分失敗 fallthrough を
    実 HTTP なしで検証する。
    """

    FEEDS = (
        "https://news.cornell.edu/taxonomy/term/24043/feed",  # AI fixture
        "https://news.cornell.edu/taxonomy/term/14248/feed",  # Health fixture (overlap)
    )

    async def _fetch_feed(self, url: str) -> bytes:
        if "24043" in url:
            return _FIXTURE_AI.read_bytes()
        return _FIXTURE_HEALTH.read_bytes()


class _PartialFailureFetcher(CornellChronicleFetcher):
    """1 feed 目が transient 失敗するシナリオ。"""

    FEEDS = (
        "https://news.cornell.edu/taxonomy/term/24043/feed",
        "https://news.cornell.edu/taxonomy/term/14248/feed",
    )

    async def _fetch_feed(self, url: str) -> bytes:
        if "24043" in url:
            raise TemporaryFetchError(f"simulated 503: {url}")
        return _FIXTURE_HEALTH.read_bytes()


async def _collect(it: AsyncIterator[FetchOutcome]) -> list[FetchOutcome]:
    return [o async for o in it]


class TestMultiFeedTraversal:
    @pytest.mark.asyncio
    async def test_dedupes_overlapping_urls_across_feeds(self) -> None:
        # AI fixture と Health fixture は cornell-tech-frontiers-ai-summit
        # を共有する → 1 度だけ yield されるはず。
        fetcher = _StubFetcher()
        outcomes = await _collect(fetcher.fetch(_SOURCE_ID))
        urls = [
            str(o.source_url.root) for o in outcomes if isinstance(o, PendingHtmlFetch)
        ]
        # AI: 3 entries (うち 1 件は pubDate 欠落、それでも Pattern H で出る)
        # Health: 2 entries (うち 1 件は AI とリンク重複)
        # 期待: AI 3 + Health 1 (新規) = 4
        assert len(urls) == 4
        assert (
            urls.count(
                "https://news.cornell.edu/stories/2026/05/cornell-tech-frontiers-ai-summit"
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_partial_feed_failure_does_not_stop_others(self) -> None:
        # 1 feed が落ちても他 feed は走る。
        fetcher = _PartialFailureFetcher()
        outcomes = await _collect(fetcher.fetch(_SOURCE_ID))
        # Health fixture の 2 entries が出てくるはず。
        pendings = [o for o in outcomes if isinstance(o, PendingHtmlFetch)]
        assert len(pendings) == 2
