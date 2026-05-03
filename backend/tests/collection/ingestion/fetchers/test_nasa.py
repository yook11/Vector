"""``NASAFetcher`` の単体テスト (Phase 1c-A1 + Phase 3 PR 3-i-2 拡張)。

per-source 設計:
- author / image_url は構造的に **常に None**
- language は **hardcoded "en-US"**
- body は ``content[0].value`` 直取り (**nav noise 含むまま**)

PR 3-i-2 拡張:
- 6 feed (本体 + 5 補強) を ``FEEDS`` ClassVar で順次巡回
- in-memory ``seen_urls: set[str]`` で同 URL の重複を排除
- 1 feed の TemporaryFetchError は warn して次 feed 続行 (全停止しない)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import feedparser
import pytest

from app.collection.errors import TemporaryFetchError
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.nasa import NASAFetcher, _extract_body

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "nasa_rss.xml"


_SOURCE_ID = 1


_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "NASA Title",
        "link": "https://www.nasa.gov/article/",
        "id": "https://www.nasa.gov/?p=1",
        "content": [{"value": "<p>" + _LOREM * 10 + "</p>"}],
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "tags": [{"term": "Astrophysics"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert NASAFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})


class TestExtractBody:
    def test_takes_content_encoded_directly(self) -> None:
        assert _extract_body({"content": [{"value": "full"}]}) == "full"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = NASAFetcher()
        self.source_id = _SOURCE_ID

    def test_author_is_always_none(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(author="Should Not Appear"), self.source_id
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author is None

    def test_image_url_is_always_none(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(media_content=[{"url": "https://example.com/x.jpg"}]),
            self.source_id,
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is None

    def test_language_hardcoded_en_us(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.language == "en-US"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Astrophysics",)

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(content=[{"value": "<p>tiny</p>"}]), self.source_id
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"


class TestFixtureParsing:
    def test_fixture_no_channel_language(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        assert feed.feed.get("language") is None

    def test_fixture_first_entry_includes_nav_noise_in_body(self) -> None:
        # 設計上の事実を test 化:
        # NASA の content:encoded には冒頭 nav menu と末尾 boilerplate が含まれ、
        # Phase 1 では受容して下流 (Stage 2 LLM) に渡す。
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = NASAFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID)
        assert isinstance(outcome, ReadyForArticle)
        assert "Earth Observatory" in outcome.article.body
        assert "Discover More Topics" in outcome.article.body

    def test_fixture_first_entry_metadata(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = NASAFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author is None
        assert outcome.metadata.image_url is None
        assert outcome.metadata.language == "en-US"
        assert "Astrophysics" in outcome.metadata.tags

    def test_fixture_second_entry_too_short_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = NASAFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"


def _build_minimal_rss(entries: list[tuple[str, str]]) -> str:
    """テスト用 RSS 2.0 文字列を組み立てる (title, link のタプル列から)。"""
    body = "Lorem ipsum dolor sit amet " * 5
    items = "\n".join(
        f"""    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="false">{link}</guid>
      <pubDate>Thu, 16 Apr 2026 12:00:00 +0000</pubDate>
      <content:encoded><![CDATA[<p>{body}</p>]]></content:encoded>
    </item>"""
        for title, link in entries
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>NASA</title>
    <link>https://www.nasa.gov/</link>
    <description>test</description>
{items}
  </channel>
</rss>"""


class TestFeedsClassVar:
    """``FEEDS`` ClassVar に本体 + 5 補強の 6 URL が並んでいることの確認。"""

    def test_feeds_count(self) -> None:
        assert len(NASAFetcher.FEEDS) == 6

    def test_feeds_includes_canonical(self) -> None:
        assert "https://www.nasa.gov/feed/" in NASAFetcher.FEEDS

    def test_feeds_includes_all_augments(self) -> None:
        for path in (
            "news-release/feed/",
            "technology/feed/",
            "aeronautics/feed/",
            "missions/station/feed/",
            "missions/artemis/feed/",
        ):
            assert any(url.endswith(path) for url in NASAFetcher.FEEDS), path

    def test_endpoint_url_is_canonical_feed(self) -> None:
        # representative 値として本体 /feed/ を残すことの確認
        assert NASAFetcher.ENDPOINT_URL == "https://www.nasa.gov/feed/"


class TestFetchTraversesFeedsAndDedups:
    """``fetch()`` が FEEDS を順次巡回し、URL 重複を in-memory dedup することの確認。"""

    @pytest.mark.asyncio
    async def test_dedups_cross_feed_urls(self) -> None:
        # feed-A と feed-B が同じ URL を持つ entry を返す → 1 回のみ yield
        shared = "https://www.nasa.gov/article/shared"
        unique_a = "https://www.nasa.gov/article/a"
        unique_b = "https://www.nasa.gov/article/b"

        feed_a_text = _build_minimal_rss(
            [("Shared Article", shared), ("Unique A", unique_a)]
        )
        feed_b_text = _build_minimal_rss(
            [("Shared Article", shared), ("Unique B", unique_b)]
        )
        # 残り 4 feed は空 RSS
        empty = _build_minimal_rss([])

        responses = [feed_a_text, feed_b_text, empty, empty, empty, empty]
        fetcher = NASAFetcher()
        fetcher._fetch_feed = AsyncMock(side_effect=responses)  # type: ignore[method-assign]

        outcomes = [o async for o in fetcher.fetch(_SOURCE_ID)]
        urls = [
            str(o.article.source_url.root)
            for o in outcomes
            if isinstance(o, ReadyForArticle)
        ]
        # shared が 1 回のみ、unique_a / unique_b が 1 回ずつ = 計 3 件
        assert urls.count(shared) == 1
        assert unique_a in urls
        assert unique_b in urls
        assert len(urls) == 3

    @pytest.mark.asyncio
    async def test_continues_on_single_feed_temporary_failure(self) -> None:
        # 1 feed が TemporaryFetchError → warn + 次 feed 続行
        ok_text = _build_minimal_rss([("Ok", "https://www.nasa.gov/article/ok")])
        responses: list[str | TemporaryFetchError] = [
            TemporaryFetchError("boom"),
            ok_text,
            ok_text,  # dedup されるので yield されない
            ok_text,
            ok_text,
            ok_text,
        ]
        fetcher = NASAFetcher()
        fetcher._fetch_feed = AsyncMock(side_effect=responses)  # type: ignore[method-assign]

        outcomes = [o async for o in fetcher.fetch(_SOURCE_ID)]
        urls = [
            str(o.article.source_url.root)
            for o in outcomes
            if isinstance(o, ReadyForArticle)
        ]
        assert urls == ["https://www.nasa.gov/article/ok"]

    @pytest.mark.asyncio
    async def test_calls_fetch_feed_for_every_feeds_entry(self) -> None:
        # 6 feed 全てに対して _fetch_feed が呼ばれること
        empty = _build_minimal_rss([])
        fetcher = NASAFetcher()
        fetcher._fetch_feed = AsyncMock(return_value=empty)  # type: ignore[method-assign]

        _ = [o async for o in fetcher.fetch(_SOURCE_ID)]
        assert fetcher._fetch_feed.call_count == 6
        called_urls = [c.args[0] for c in fetcher._fetch_feed.call_args_list]
        assert set(called_urls) == set(NASAFetcher.FEEDS)
