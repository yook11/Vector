"""``ITmediaAIFetcher`` の単体テスト (Phase 1c-C)。

per-source 設計:
- title から `[ITmedia ...]` 接頭辞を per-source regex で除去
- author / tags / image_url / guid は ``None`` / ``()`` 直書き
- PROVIDES から ``guid`` を除外 (RSS が ``<guid>`` を提供しない)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.itmedia_ai import (
    ITmediaAIFetcher,
    _normalize_language,
    _strip_title_prefix,
)
from app.models.news_source import NewsSource

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "itmedia_ai_rss.xml"
)


def _source(source_id: int = 1, name: str = "ITmedia AI+") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml"
    return s


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "[ITmedia PC USER] Claude が新機能を追加",
        "link": "https://www.itmedia.co.jp/aiplus/articles/2604/30/news001.html",
        "summary": "Anthropic は AI アシスタント Claude の新機能を発表した。",
        "published_parsed": time.struct_time((2026, 4, 30, 14, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        # ITmedia AI+ は <guid> を提供しないため PROVIDES から除外
        assert ITmediaAIFetcher.PROVIDES == frozenset({"language", "site_name"})


class TestStripTitlePrefix:
    def test_removes_simple_prefix(self) -> None:
        assert _strip_title_prefix("[ITmedia PC USER] Foo") == "Foo"

    def test_removes_multibyte_section(self) -> None:
        assert _strip_title_prefix("[ITmedia エンタープライズ] Bar") == "Bar"

    def test_no_op_when_no_prefix(self) -> None:
        assert _strip_title_prefix("Foo Bar") == "Foo Bar"

    def test_only_first_prefix_removed(self) -> None:
        # 1 度だけ除去 (count=1)
        assert _strip_title_prefix("[A][B] X") == "[B] X"


class TestNormalizeLanguage:
    def test_default_when_missing(self) -> None:
        assert _normalize_language(None) == "ja"

    def test_passthrough(self) -> None:
        assert _normalize_language("ja") == "ja"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = ITmediaAIFetcher()
        self.source = _source()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "Claude が新機能を追加"  # 接頭辞除去済み
        assert outcome.source_id == 1
        assert outcome.published_at_hint is not None

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source, "ja"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_missing_published_yields_pending_with_none(self) -> None:
        e = _entry()
        del e["published_parsed"]
        outcome = self.fetcher._convert_entry(e, self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_minimum(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author is None
        assert outcome.metadata.tags == ()
        assert outcome.metadata.image_url is None
        assert outcome.metadata.guid is None
        assert outcome.metadata.language == "ja"
        assert outcome.metadata.site_name == "ITmedia AI+"


class TestFixtureParsing:
    def test_fixture_first_entry_strips_prefix(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = ITmediaAIFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("「Claude」が新機能を追加")
        assert "[ITmedia" not in outcome.title
        assert outcome.metadata.language == "ja"
        assert outcome.metadata.guid is None  # RSS 提供なし

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = ITmediaAIFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _source(), "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
