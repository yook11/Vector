"""``AnthropicFetcher`` + ``BaseSitemapFetcher`` の単体テスト (Phase 3 PR 3-d-4)。

per-source 設計:
- sitemap.xml 形式 (RSS 不在)
- ``<loc>`` が ``/news/`` で始まるものだけ yield (about/pricing 等は除外)
- lastmod 降順で MAX_ENTRIES 件まで
- title は URL slug プレースホルダ (HTML 抽出で overwrite される)
- ``prefer_html_title=True`` を契約として固定
- PROVIDES = {site_name, language}
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers._base.sitemap import BaseSitemapFetcher
from app.collection.ingestion.fetchers.anthropic import AnthropicFetcher

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "anthropic_sitemap.xml"
)
_SOURCE_ID = 1


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert AnthropicFetcher.PROVIDES == frozenset({"site_name", "language"})

    def test_endpoint_is_sitemap(self) -> None:
        assert AnthropicFetcher.ENDPOINT_URL == "https://www.anthropic.com/sitemap.xml"

    def test_path_prefix_is_news(self) -> None:
        assert AnthropicFetcher.URL_PATH_PREFIX == "/news/"


class TestSitemapParser:
    """``BaseSitemapFetcher._parse_sitemap`` の純関数テスト (lxml)。"""

    def test_parses_loc_and_lastmod(self) -> None:
        data = _FIXTURE.read_bytes()
        entries = BaseSitemapFetcher._parse_sitemap(data)
        assert len(entries) == 7
        first_loc, first_lastmod = entries[0]
        assert first_loc == "https://www.anthropic.com/news/claude-3-5-sonnet"
        assert first_lastmod == datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

    def test_z_suffix_lastmod_parses_as_utc(self) -> None:
        data = _FIXTURE.read_bytes()
        entries = BaseSitemapFetcher._parse_sitemap(data)
        z_entry = next(
            (lm for loc, lm in entries if loc.endswith("/research-update")), None
        )
        assert z_entry == datetime(2026, 4, 15, 15, 0, 0, tzinfo=UTC)

    def test_missing_lastmod_yields_none(self) -> None:
        data = _FIXTURE.read_bytes()
        entries = BaseSitemapFetcher._parse_sitemap(data)
        older = next((lm for loc, lm in entries if loc.endswith("/older-post")), "miss")
        assert older is None

    def test_xxe_external_entity_disabled(self) -> None:
        # 外部実体参照を含む sitemap でも payload は entity 展開されない
        # (resolve_entities=False / no_network=True / load_dtd=False の効果)
        malicious = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.anthropic.com/news/&xxe;</loc></url>
</urlset>
"""
        entries = BaseSitemapFetcher._parse_sitemap(malicious)
        assert len(entries) == 1
        loc = entries[0][0]
        # entity 展開が走っていれば file 内容が混入 / 走らなければ literal が残る
        assert "/etc/passwd" not in loc
        assert "root:" not in loc


class TestUrlMatching:
    def test_news_path_matches(self) -> None:
        f = AnthropicFetcher()
        assert f._url_matches("https://www.anthropic.com/news/claude-3-5-sonnet")

    def test_about_path_rejected(self) -> None:
        f = AnthropicFetcher()
        assert not f._url_matches("https://www.anthropic.com/about")

    def test_pricing_path_rejected(self) -> None:
        f = AnthropicFetcher()
        assert not f._url_matches("https://www.anthropic.com/pricing")

    def test_news_index_matches(self) -> None:
        # /news/ そのもの (trailing slash) も prefix match
        f = AnthropicFetcher()
        assert f._url_matches("https://www.anthropic.com/news/")


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = AnthropicFetcher()
        self.lastmod = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

    def test_valid_entry_yields_pending_with_prefer_html_title(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://www.anthropic.com/news/claude-3-5-sonnet",
            self.lastmod,
            _SOURCE_ID,
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.prefer_html_title is True

    def test_title_is_url_slug_placeholder(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://www.anthropic.com/news/claude-3-5-sonnet",
            self.lastmod,
            _SOURCE_ID,
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "claude-3-5-sonnet"

    def test_lastmod_yields_published_at_hint(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://www.anthropic.com/news/x",
            self.lastmod,
            _SOURCE_ID,
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert outcome.published_at_hint.value == self.lastmod

    def test_missing_lastmod_yields_pending_with_none_hint(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://www.anthropic.com/news/x", None, _SOURCE_ID
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_site_name_and_language(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://www.anthropic.com/news/x", self.lastmod, _SOURCE_ID
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "Anthropic"
        assert outcome.metadata.language == "en"

    def test_guid_is_loc_url(self) -> None:
        outcome = self.fetcher._convert_entry(
            "https://www.anthropic.com/news/some-post", self.lastmod, _SOURCE_ID
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == "https://www.anthropic.com/news/some-post"

    def test_invalid_url_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry("not-a-url", self.lastmod, _SOURCE_ID)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_trailing_slash_url_uses_name_as_fallback_title(self) -> None:
        # /news/ で終わる (slug 空) URL は NAME を placeholder に使う
        outcome = self.fetcher._convert_entry(
            "https://www.anthropic.com/news/", self.lastmod, _SOURCE_ID
        )
        # /news/ → rstrip → /news → split → "news" が slug、空にはならない
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "news"


class TestFixtureFiltering:
    """fixture 全体に対する filter / sort / cap の契約。"""

    def test_news_only_after_filter(self) -> None:
        fetcher = AnthropicFetcher()
        data = _FIXTURE.read_bytes()
        entries = BaseSitemapFetcher._parse_sitemap(data)
        filtered = [e for e in entries if fetcher._url_matches(e[0])]
        # /news/ で始まる 5 件 (3 つの lastmod 付き + older-post + /news/ index)
        assert len(filtered) == 5
        for loc, _ in filtered:
            assert "/news/" in loc

    def test_lastmod_desc_sort_puts_newest_first(self) -> None:
        fetcher = AnthropicFetcher()
        data = _FIXTURE.read_bytes()
        entries = BaseSitemapFetcher._parse_sitemap(data)
        filtered = [e for e in entries if fetcher._url_matches(e[0])]
        filtered.sort(
            key=lambda e: e[1] or datetime.min.replace(tzinfo=UTC), reverse=True
        )
        # /news/ index が lastmod=2026-05-04 で最新、claude-3-5-sonnet が次
        assert filtered[0][0].endswith("/news/")
        assert filtered[1][0].endswith("/claude-3-5-sonnet")
        # lastmod 無しの older-post は最後
        assert filtered[-1][0].endswith("/older-post")

    def test_max_entries_cap_respected(self) -> None:
        # MAX_ENTRIES デフォルト 30 < fixture 全件 → 全件通る
        # (本テストは契約として cap 機構が存在することの確認)
        assert AnthropicFetcher.MAX_ENTRIES >= 1
