"""``ORNLNewsFetcher`` の単体テスト (Phase 3 PR 3-i-1)。

per-source 設計検証:
- listing HTML から /news/ 配下の link を XPath 抽出
- ``EXCLUDED_PATHS`` で 6 件の category landing を除外
- 重複 URL は ``seen`` set で dedup
- ``PROVIDES = {site_name, language}`` (HTML listing 経路は title/published_at
  を Stage 2 に委ねる)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.collection.ingestion.domain.fetched_article import PendingHtmlFetch
from app.collection.ingestion.fetchers.ornl import ORNLNewsFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "ornl_listing.html"

_SOURCE_ID = 1


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert ORNLNewsFetcher.PROVIDES == frozenset({"site_name", "language"})

    def test_excluded_paths_cover_known_categories(self) -> None:
        # 2026-05-04 実 listing で確認した 6 件
        assert "/news/releases" in ORNLNewsFetcher.EXCLUDED_PATHS
        assert "/news/features" in ORNLNewsFetcher.EXCLUDED_PATHS
        assert "/news/researcher-profiles" in ORNLNewsFetcher.EXCLUDED_PATHS
        assert "/news/story-tips" in ORNLNewsFetcher.EXCLUDED_PATHS
        assert "/news/audio-spots" in ORNLNewsFetcher.EXCLUDED_PATHS
        assert "/news/honors-and-awards" in ORNLNewsFetcher.EXCLUDED_PATHS


class TestParseListingFixture:
    def test_xpath_extracts_news_links(self) -> None:
        data = _FIXTURE.read_bytes()
        urls = ORNLNewsFetcher._parse_listing(data)
        # 6 categories + 3 articles + 1 duplicate = 10 raw matches
        # (external link is excluded by XPath)
        assert len(urls) == 10
        assert all(url.startswith("https://www.ornl.gov/news/") for url in urls)


class TestFetchPipelineFixture:
    @pytest.mark.asyncio
    async def test_fetch_excludes_categories_and_dedups(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = _FIXTURE.read_bytes()

        async def _fake_fetch_listing(self: ORNLNewsFetcher) -> bytes:
            return data

        monkeypatch.setattr(
            ORNLNewsFetcher, "_fetch_listing", _fake_fetch_listing, raising=True
        )

        fetcher = ORNLNewsFetcher()
        outcomes: list[object] = []
        async for o in fetcher.fetch(_SOURCE_ID):
            outcomes.append(o)

        # Expected: 3 unique articles (6 categories excluded, 1 duplicate dedup'd)
        assert len(outcomes) == 3
        for outcome in outcomes:
            assert isinstance(outcome, PendingHtmlFetch)
            assert outcome.metadata.site_name == "ORNL"
            assert outcome.metadata.language == "en"
            assert outcome.prefer_html_title is True

    @pytest.mark.asyncio
    async def test_fetch_yields_full_news_urls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = _FIXTURE.read_bytes()

        async def _fake_fetch_listing(self: ORNLNewsFetcher) -> bytes:
            return data

        monkeypatch.setattr(
            ORNLNewsFetcher, "_fetch_listing", _fake_fetch_listing, raising=True
        )

        fetcher = ORNLNewsFetcher()
        urls: list[str] = []
        async for o in fetcher.fetch(_SOURCE_ID):
            assert isinstance(o, PendingHtmlFetch)
            urls.append(str(o.source_url))

        assert (
            "https://www.ornl.gov/news/biosensor-detects-early-fungal-outbreaks-"
            "advances-plant-biotechnology" in urls
        )
        assert (
            "https://www.ornl.gov/news/photon-framework-scales-ai-vulnerability-discovery"
            in urls
        )
        assert (
            "https://www.ornl.gov/news/imaging-innovation-advances-nuclear-materials-qualification"
            in urls
        )
