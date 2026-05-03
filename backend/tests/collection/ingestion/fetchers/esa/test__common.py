"""``BaseDjangoplicityFetcher`` の helper / 振る舞い単体テスト (Phase 3 PR 3-b)。

base class そのものはインスタンス化しないが、``ClassVar`` を持った最小
fixture subclass を作って ``_convert_entry`` の挙動を検証する。
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.esa._common import (
    BaseDjangoplicityFetcher,
    _normalize_language,
)


class _DummyFetcher(BaseDjangoplicityFetcher):
    NAME: ClassVar[str] = "Dummy"
    ENDPOINT_URL: ClassVar[str] = "https://dummy.example.com/feed/"
    SITE_NAME: ClassVar[str] = "Dummy Site"
    AUTHOR: ClassVar[str] = "Dummy Org"


_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Photo Release: Dummy Title",
        "link": "https://dummy.example.com/news/abc/",
        "id": "https://dummy.example.com/news/abc/",
        "published_parsed": time.struct_time((2026, 4, 20, 16, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestNormalizeLanguage:
    def test_default_when_none(self) -> None:
        assert _normalize_language(None, default="en") == "en"

    def test_underscore_to_hyphen(self) -> None:
        assert _normalize_language("en_US", default="en") == "en-US"

    def test_truncates_to_20_chars(self) -> None:
        assert len(_normalize_language("a" * 50, default="en")) == 20


class TestProvides:
    def test_provides_includes_author(self) -> None:
        # author hardcode のため PROVIDES に含む
        assert _DummyFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name", "author"}
        )


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = _DummyFetcher()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "Photo Release: Dummy Title"

    def test_does_not_construct_body(self) -> None:
        # Pattern H: 本文は HTML 抽出 task の責務、Fetcher は触らない
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), _SOURCE_ID, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), _SOURCE_ID, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H: 緩い品質ゲート、HTML 補完を待つ
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_from_classvar(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "Dummy Org"

    def test_metadata_site_name_from_classvar(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "Dummy Site"

    def test_metadata_image_url_hardcoded_none(self) -> None:
        # Djangoplicity feed は image を提供しない、HTML 抽出に委譲
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_metadata_tags_hardcoded_empty(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ()

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == "https://dummy.example.com/news/abc/"

    def test_language_passthrough(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "en"
