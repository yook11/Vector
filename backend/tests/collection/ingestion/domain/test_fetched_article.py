"""ingestion BC 出口型の不変条件テスト。

検証する不変条件:

- ``ReadyForArticle`` は永続化 passport の 5 fields を strict に通すこと
- ``IncompleteArticle`` は kiq message に乗せる前提 (frozen BaseModel) を満たすこと
- ``try_advance_from`` の Pattern H promotion 規則 (RSS preferred / HTML fallback /
  両欠落で Failed)
- ``FetchedEntry`` envelope は item + opaque metadata を運ぶ Service-internal 型

実装枚挙 (Optional フィールド数 / 個別バリデータ等) は書かない。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FetchedEntry,
    IncompleteArticle,
    ReadyForArticle,
)
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


def _pub() -> PublishedAt:
    return PublishedAt(value=datetime(2026, 4, 30, 0, 0, 0, tzinfo=UTC))


def _url(s: str = "https://example.com/a") -> CanonicalArticleUrl:
    return CanonicalArticleUrl(s)


def _ready(**overrides: object) -> ReadyForArticle:
    base: dict[str, object] = {
        "title": "Test",
        "body": "x" * 100,
        "published_at": _pub(),
        "source_id": 1,
        "source_url": _url(),
    }
    base.update(overrides)
    return ReadyForArticle(**base)  # type: ignore[arg-type]


def _pending(**overrides: object) -> IncompleteArticle:
    base: dict[str, object] = {
        "title": "Test",
        "source_id": 1,
        "source_url": _url(),
    }
    base.update(overrides)
    return IncompleteArticle(**base)  # type: ignore[arg-type]


class TestReadyForArticle:
    """永続化 passport — 5 strict fields を通すこと、frozen であることだけ確認する。"""

    def test_constructs_with_minimal_valid_input(self) -> None:
        ready = _ready()
        assert ready.title == "Test"
        assert ready.source_id == 1

    def test_rejects_inputs_violating_persistence_invariants(self) -> None:
        with pytest.raises(ValueError):
            _ready(title="")
        with pytest.raises(ValueError):
            _ready(body="x" * 49)
        with pytest.raises(ValueError):
            _ready(source_id=0)

    def test_is_frozen_to_protect_passport_integrity(self) -> None:
        ready = _ready()
        with pytest.raises(ValueError):
            ready.title = "Changed"  # type: ignore[misc]


class TestIncompleteArticle:
    """Stage 2 への kiq 引数。frozen BaseModel + invariants の境界だけ確認する。"""

    def test_constructs_with_minimal_valid_input(self) -> None:
        pending = _pending()
        assert pending.title == "Test"
        assert pending.published_at_hint is None
        assert pending.prefer_html_title is False

    def test_rejects_inputs_violating_invariants(self) -> None:
        with pytest.raises(ValueError):
            _pending(title="")
        with pytest.raises(ValueError):
            _pending(source_id=0)

    def test_is_frozen_for_kiq_safety(self) -> None:
        pending = _pending()
        with pytest.raises(ValueError):
            pending.title = "Changed"  # type: ignore[misc]


class TestTryAdvanceFromPromotion:
    """Pattern H 1 段目 → 2 段目 promotion 規則 (Stage 2 が呼ぶ唯一の API)。"""

    def test_rss_published_at_preferred_over_html(self) -> None:
        rss_pub = PublishedAt(value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC))
        html_pub = PublishedAt(value=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC))
        result = ReadyForArticle.try_advance_from(
            _pending(published_at_hint=rss_pub),
            body="x" * 100,
            html_published_at=html_pub,
        )
        assert isinstance(result, ReadyForArticle)
        assert result.published_at == rss_pub

    def test_html_published_at_used_as_fallback_when_rss_missing(self) -> None:
        html_pub = PublishedAt(value=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC))
        result = ReadyForArticle.try_advance_from(
            _pending(published_at_hint=None),
            body="x" * 100,
            html_published_at=html_pub,
        )
        assert isinstance(result, ReadyForArticle)
        assert result.published_at == html_pub

    def test_failed_when_both_published_at_missing(self) -> None:
        result = ReadyForArticle.try_advance_from(
            _pending(published_at_hint=None),
            body="x" * 100,
            html_published_at=None,
        )
        assert isinstance(result, Failed)
        assert result.reason.code == "published_at_missing"

    def test_failed_when_html_body_violates_invariants(self) -> None:
        rss_pub = PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC))
        result = ReadyForArticle.try_advance_from(
            _pending(published_at_hint=rss_pub),
            body="too short",
            html_published_at=None,
        )
        assert isinstance(result, Failed)
        assert result.reason.code == "other"

    def test_html_title_used_only_when_prefer_html_title(self) -> None:
        rss_pub = PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC))
        rss_only = ReadyForArticle.try_advance_from(
            _pending(title="RSS Title", published_at_hint=rss_pub),
            body="x" * 100,
            html_published_at=None,
            html_title="HTML Title",
        )
        assert isinstance(rss_only, ReadyForArticle)
        assert rss_only.title == "RSS Title"

        html_first = ReadyForArticle.try_advance_from(
            _pending(
                title="placeholder-slug",
                published_at_hint=rss_pub,
                prefer_html_title=True,
            ),
            body="x" * 100,
            html_published_at=None,
            html_title="HTML Title",
        )
        assert isinstance(html_first, ReadyForArticle)
        assert html_first.title == "HTML Title"


class TestFetchedEntry:
    """Service-internal envelope。item + opaque metadata の運搬だけ確認。"""

    def test_carries_ready_for_article(self) -> None:
        entry = FetchedEntry(item=_ready(), metadata={"language": "en"})
        assert isinstance(entry.item, ReadyForArticle)
        assert entry.metadata["language"] == "en"

    def test_carries_pending_html_fetch(self) -> None:
        entry = FetchedEntry(item=_pending(), metadata={"site_name": "X"})
        assert isinstance(entry.item, IncompleteArticle)
        assert entry.metadata["site_name"] == "X"

    def test_metadata_is_opaque_dict(self) -> None:
        # Fetcher ごとに異なる key を入れて良いこと (型による制約なし)
        entry = FetchedEntry(
            item=_ready(),
            metadata={"language": "en", "doi": "10.1/x", "score": 42},
        )
        assert set(entry.metadata.keys()) == {"language", "doi", "score"}
