"""``BaseHtmlListingFetcher`` の単体テスト (Phase 3 PR 3-i-1)。

per-base 設計検証:
- XPath で href を抽出し ``urljoin`` で絶対 URL 化する
- ``EXCLUDED_PATHS`` で listing 内 category landing 等を除外する
- 重複 URL を ``seen`` set で dedup する
- ``MAX_ENTRIES`` で件数を cap する
- ``_convert_entry`` は ``prefer_html_title=True`` の ``PendingHtmlFetch``
  を返し、title は URL slug プレースホルダ
- 不正 URL は ``Failed(extraction_empty)`` で個別 drop (全体停止しない)
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers._base.html_listing import BaseHtmlListingFetcher

_SOURCE_ID = 1


class _SampleFetcher(BaseHtmlListingFetcher):
    """テスト用 subclass。実 fetch は呼ばず、parse / convert / filter のみ
    検証する。"""

    NAME: ClassVar[str] = "Sample"
    ENDPOINT_URL: ClassVar[str] = "https://example.com/news"
    LISTING_URL: ClassVar[str] = "https://example.com/news"
    DETAIL_LINK_XPATH: ClassVar[str] = '//a[starts-with(@href, "/news/")]'
    DETAIL_URL_PREFIX: ClassVar[str] = "https://example.com"
    SITE_NAME: ClassVar[str] = "Sample"
    LANGUAGE: ClassVar[str] = "en"
    EXCLUDED_PATHS: ClassVar[frozenset[str]] = frozenset({"/news/categories"})
    MAX_ENTRIES: ClassVar[int] = 10


_FIXTURE_HTML = b"""<!DOCTYPE html>
<html><body>
  <a href="/news/categories">Categories landing (excluded)</a>
  <a href="/news/article-one">Article One</a>
  <a href="/news/article-two">Article Two</a>
  <a href="/news/article-one">Duplicate of Article One</a>
  <a href="https://example.com/external">External link (XPath excludes)</a>
</body></html>
"""


class TestParseListing:
    def test_xpath_extracts_only_news_links(self) -> None:
        urls = _SampleFetcher._parse_listing(_FIXTURE_HTML)
        # External /external link is excluded by XPath itself
        assert all(url.startswith("https://example.com/news/") for url in urls)

    def test_xpath_resolves_relative_urls(self) -> None:
        urls = _SampleFetcher._parse_listing(_FIXTURE_HTML)
        assert "https://example.com/news/article-one" in urls

    def test_xpath_returns_duplicates_pre_dedup(self) -> None:
        # _parse_listing 自体は dedup しない (fetch ループで dedup)
        urls = _SampleFetcher._parse_listing(_FIXTURE_HTML)
        assert urls.count("https://example.com/news/article-one") == 2


class TestUrlMatches:
    def test_excluded_paths_filter(self) -> None:
        fetcher = _SampleFetcher()
        assert fetcher._url_matches("https://example.com/news/article-one") is True
        assert fetcher._url_matches("https://example.com/news/categories") is False


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = _SampleFetcher()

    def test_yields_pending_with_prefer_html_title(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://example.com/news/article-one", _SOURCE_ID
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.prefer_html_title is True

    def test_title_is_url_slug_placeholder(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://example.com/news/article-one", _SOURCE_ID
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "article-one"

    def test_published_at_hint_is_none(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://example.com/news/article-one", _SOURCE_ID
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_site_name_and_language_set(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://example.com/news/article-one", _SOURCE_ID
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "Sample"
        assert outcome.metadata.language == "en"
        assert outcome.metadata.guid == "https://example.com/news/article-one"

    def test_invalid_url_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry("not-a-url", _SOURCE_ID)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"


class TestFetchPipeline:
    @pytest.mark.asyncio
    async def test_fetch_dedups_excludes_and_caps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = _SampleFetcher()

        async def _fake_fetch_listing(self: _SampleFetcher) -> bytes:
            return _FIXTURE_HTML

        monkeypatch.setattr(
            _SampleFetcher, "_fetch_listing", _fake_fetch_listing, raising=True
        )

        outcomes: list[object] = []
        async for o in fetcher.fetch(_SOURCE_ID):
            outcomes.append(o)

        # Expected: article-one + article-two (categories excluded, dup dedup'd,
        # external link not matched by XPath)
        assert len(outcomes) == 2
        urls = [
            str(o.source_url) if isinstance(o, PendingHtmlFetch) else None
            for o in outcomes
        ]
        assert "https://example.com/news/article-one" in urls
        assert "https://example.com/news/article-two" in urls

    @pytest.mark.asyncio
    async def test_fetch_respects_max_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        many_links = b"".join(
            f'<a href="/news/article-{i}">A{i}</a>'.encode() for i in range(50)
        )
        many_html = b"<!DOCTYPE html><html><body>" + many_links + b"</body></html>"

        class _Capped(_SampleFetcher):
            MAX_ENTRIES: ClassVar[int] = 5

        async def _fake_fetch_listing(self: _Capped) -> bytes:
            return many_html

        monkeypatch.setattr(
            _Capped, "_fetch_listing", _fake_fetch_listing, raising=True
        )

        fetcher = _Capped()
        outcomes: list[object] = []
        async for o in fetcher.fetch(_SOURCE_ID):
            outcomes.append(o)

        assert len(outcomes) == 5
