"""``FetchedArticle`` / ``FetchedMetadata`` / ``FetchOutcome`` の invariant テスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import pytest

from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FailureReason,
    FetchedArticle,
    FetchedMetadata,
    PendingHtmlFetch,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.protocol import Fetcher
from app.shared.value_objects.safe_url import SafeUrl


def _published_at() -> PublishedAt:
    return PublishedAt(value=datetime(2026, 4, 30, 0, 0, 0, tzinfo=UTC))


def _safe_url(url: str = "https://example.com/article") -> SafeUrl:
    return SafeUrl(url)


class TestFetchedArticle:
    def test_accepts_minimal_valid_input(self) -> None:
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        assert article.title == "Test"
        assert article.source_id == 1

    def test_rejects_empty_title(self) -> None:
        with pytest.raises(ValueError):
            FetchedArticle(
                title="",
                body="x" * 50,
                published_at=_published_at(),
                source_id=1,
                source_url=_safe_url(),
            )

    def test_rejects_title_over_500_chars(self) -> None:
        with pytest.raises(ValueError):
            FetchedArticle(
                title="x" * 501,
                body="x" * 50,
                published_at=_published_at(),
                source_id=1,
                source_url=_safe_url(),
            )

    def test_rejects_body_under_50_chars(self) -> None:
        with pytest.raises(ValueError):
            FetchedArticle(
                title="Test",
                body="x" * 49,
                published_at=_published_at(),
                source_id=1,
                source_url=_safe_url(),
            )

    def test_rejects_non_positive_source_id(self) -> None:
        with pytest.raises(ValueError):
            FetchedArticle(
                title="Test",
                body="x" * 50,
                published_at=_published_at(),
                source_id=0,
                source_url=_safe_url(),
            )

    def test_is_frozen(self) -> None:
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        with pytest.raises(ValueError):
            article.title = "Changed"  # type: ignore[misc]


class TestFailureReason:
    def test_accepts_valid_code(self) -> None:
        reason = FailureReason(code="http_transient", retryable=True)
        assert reason.code == "http_transient"
        assert reason.retryable is True
        assert reason.detail is None

    def test_accepts_detail(self) -> None:
        reason = FailureReason(
            code="published_at_missing",
            retryable=False,
            detail="rss_pubdate_missing",
        )
        assert reason.detail == "rss_pubdate_missing"

    def test_is_frozen(self) -> None:
        reason = FailureReason(code="http_transient", retryable=True)
        with pytest.raises(ValueError):
            reason.code = "http_blocked"  # type: ignore[misc]


class TestFetchOutcome:
    def test_ready_carries_article(self) -> None:
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        outcome = ReadyForArticle(article=article)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article is article

    def test_failed_carries_reason(self) -> None:
        reason = FailureReason(code="extraction_empty", retryable=False)
        outcome = Failed(reason=reason)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_match_dispatch(self) -> None:
        """Union 型を ``match`` で分岐できる (上流 Service の典型用法)。"""
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        outcomes = [
            ReadyForArticle(article=article),
            Failed(reason=FailureReason(code="paywalled", retryable=False)),
        ]
        ready_count = 0
        failed_codes: list[str] = []
        for outcome in outcomes:
            match outcome:
                case ReadyForArticle(article=a):
                    ready_count += 1
                    assert a.title == "Test"
                case Failed(reason=r):
                    failed_codes.append(r.code)
        assert ready_count == 1
        assert failed_codes == ["paywalled"]


class TestFetchedMetadata:
    def test_default_all_none_or_empty(self) -> None:
        metadata = FetchedMetadata()
        assert metadata.summary is None
        assert metadata.author is None
        assert metadata.authors == ()
        assert metadata.tags == ()
        assert metadata.categories == ()
        assert metadata.image_url is None
        assert metadata.language is None
        assert metadata.guid is None
        assert metadata.updated_at is None
        assert metadata.site_name is None
        assert metadata.extras is None

    def test_all_fields_populated(self) -> None:
        metadata = FetchedMetadata(
            summary="A short summary.",
            author="Jane Doe",
            authors=("Jane Doe", "John Smith"),
            tags=("ai", "nlp"),
            categories=("Technology",),
            image_url=SafeUrl("https://example.com/cover.jpg"),
            language="en-US",
            guid="https://example.com/article",
            updated_at=_published_at(),
            site_name="Example",
            extras={"word_count": 1234},
        )
        assert metadata.summary == "A short summary."
        assert metadata.author == "Jane Doe"
        assert metadata.authors == ("Jane Doe", "John Smith")
        assert metadata.tags == ("ai", "nlp")
        assert metadata.categories == ("Technology",)
        assert str(metadata.image_url) == "https://example.com/cover.jpg"
        assert metadata.language == "en-US"
        assert metadata.guid == "https://example.com/article"
        assert metadata.updated_at == _published_at()
        assert metadata.site_name == "Example"
        assert metadata.extras == {"word_count": 1234}

    def test_summary_max_length_2000(self) -> None:
        with pytest.raises(ValueError):
            FetchedMetadata(summary="x" * 2001)

    def test_author_max_length_200(self) -> None:
        with pytest.raises(ValueError):
            FetchedMetadata(author="x" * 201)

    def test_language_max_length_20(self) -> None:
        with pytest.raises(ValueError):
            FetchedMetadata(language="x" * 21)

    def test_guid_max_length_2048(self) -> None:
        with pytest.raises(ValueError):
            FetchedMetadata(guid="x" * 2049)

    def test_site_name_max_length_100(self) -> None:
        with pytest.raises(ValueError):
            FetchedMetadata(site_name="x" * 101)

    def test_image_url_invalid_safeurl_raises(self) -> None:
        with pytest.raises(ValueError):
            FetchedMetadata(image_url="not a url")  # type: ignore[arg-type]

    def test_authors_tuple_immutable(self) -> None:
        metadata = FetchedMetadata(authors=("a", "b"))
        assert metadata.authors == ("a", "b")
        assert isinstance(metadata.authors, tuple)

    def test_extras_accepts_arbitrary_dict(self) -> None:
        metadata = FetchedMetadata(extras={"points": 42, "comments": 10})
        assert metadata.extras == {"points": 42, "comments": 10}

    def test_updated_at_naive_datetime_rejected(self) -> None:
        """``updated_at`` の UTC 強制は ``PublishedAt`` VO を経由して効く。"""
        with pytest.raises(ValueError):
            PublishedAt(value=datetime(2026, 4, 30, 0, 0, 0))

    def test_is_frozen(self) -> None:
        metadata = FetchedMetadata(summary="initial")
        with pytest.raises(ValueError):
            metadata.summary = "changed"  # type: ignore[misc]


class TestReady:
    def test_ready_with_explicit_metadata(self) -> None:
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        metadata = FetchedMetadata(summary="hi", language="en")
        ready = ReadyForArticle(article=article, metadata=metadata)
        assert ready.article is article
        assert ready.metadata is metadata

    def test_ready_default_metadata_empty(self) -> None:
        """``metadata`` 省略時、空の ``FetchedMetadata`` がデフォルトで入る。"""
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        ready = ReadyForArticle(article=article)
        assert isinstance(ready.metadata, FetchedMetadata)
        assert ready.metadata == FetchedMetadata()

    def test_ready_metadata_carries_values(self) -> None:
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        ready = ReadyForArticle(
            article=article,
            metadata=FetchedMetadata(tags=("ai",), site_name="Example"),
        )
        assert ready.metadata.tags == ("ai",)
        assert ready.metadata.site_name == "Example"


class TestPendingHtmlFetch:
    """Pattern H 1 段目の出口型 ``PendingHtmlFetch``。"""

    def test_minimal_construction(self) -> None:
        pending = PendingHtmlFetch(
            title="Test",
            source_id=1,
            source_url=_safe_url(),
        )
        assert pending.title == "Test"
        assert pending.source_id == 1
        assert pending.published_at_hint is None
        assert pending.metadata == FetchedMetadata()

    def test_published_at_hint_optional(self) -> None:
        """Pattern H 固有: published_at_hint=None が許容される (HTML 補完前)。"""
        pending = PendingHtmlFetch(
            title="Test",
            source_id=1,
            source_url=_safe_url(),
            published_at_hint=None,
        )
        assert pending.published_at_hint is None

    def test_metadata_carries_rss_capture(self) -> None:
        metadata = FetchedMetadata(language="en-US", guid="g1", site_name="TC")
        pending = PendingHtmlFetch(
            title="Test",
            source_id=1,
            source_url=_safe_url(),
            metadata=metadata,
        )
        assert pending.metadata == metadata

    def test_rejects_empty_title(self) -> None:
        with pytest.raises(ValueError):
            PendingHtmlFetch(title="", source_id=1, source_url=_safe_url())

    def test_rejects_non_positive_source_id(self) -> None:
        with pytest.raises(ValueError):
            PendingHtmlFetch(title="Test", source_id=0, source_url=_safe_url())

    def test_is_frozen(self) -> None:
        pending = PendingHtmlFetch(title="Test", source_id=1, source_url=_safe_url())
        with pytest.raises(ValueError):
            pending.title = "Changed"  # type: ignore[misc]


class TestReadyForArticleTryAdvanceFrom:
    """``ReadyForArticle.try_advance_from`` の merge 規則 (RSS 優先 / HTML 補完)。"""

    def _pending(
        self,
        published_at_hint: PublishedAt | None,
        metadata: FetchedMetadata | None = None,
    ) -> PendingHtmlFetch:
        return PendingHtmlFetch(
            title="RSS Title",
            source_id=1,
            source_url=_safe_url(),
            published_at_hint=published_at_hint,
            metadata=metadata or FetchedMetadata(language="en-US", site_name="TC"),
        )

    def test_merge_with_rss_published_at_preferred(self) -> None:
        """RSS と HTML 両方 published_at あるとき RSS が優先される。"""
        rss_pub = PublishedAt(value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC))
        html_pub = PublishedAt(value=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC))
        pending = self._pending(published_at_hint=rss_pub)

        result = ReadyForArticle.try_advance_from(
            pending, body="x" * 100, html_published_at=html_pub
        )

        assert isinstance(result, ReadyForArticle)
        assert result.article.published_at == rss_pub  # RSS 優先

    def test_merge_falls_back_to_html_when_rss_missing(self) -> None:
        """RSS が published_at を出さないとき HTML 由来でフォールバック。"""
        html_pub = PublishedAt(value=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC))
        pending = self._pending(published_at_hint=None)

        result = ReadyForArticle.try_advance_from(
            pending, body="x" * 100, html_published_at=html_pub
        )

        assert isinstance(result, ReadyForArticle)
        assert result.article.published_at == html_pub

    def test_merge_failed_when_both_missing(self) -> None:
        """RSS と HTML 両方欠落 → Failed(published_at_missing) で降格。"""
        pending = self._pending(published_at_hint=None)

        result = ReadyForArticle.try_advance_from(
            pending, body="x" * 100, html_published_at=None
        )

        assert isinstance(result, Failed)
        assert result.reason.code == "published_at_missing"

    def test_merge_failed_when_body_too_short(self) -> None:
        """body invariant 違反 → Failed(other) で降格。"""
        rss_pub = PublishedAt(value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC))
        pending = self._pending(published_at_hint=rss_pub)

        result = ReadyForArticle.try_advance_from(
            pending, body="short", html_published_at=None
        )

        assert isinstance(result, Failed)
        assert result.reason.code == "other"
        assert result.reason.detail is not None
        assert "invariant_violation" in result.reason.detail

    def test_merge_uses_pending_title_and_metadata(self) -> None:
        """title / metadata は pending (RSS) からそのまま採用される。"""
        rss_pub = PublishedAt(value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC))
        metadata = FetchedMetadata(
            author="Jane",
            tags=("AI", "Funding"),
            language="en-US",
            site_name="TC",
        )
        pending = self._pending(published_at_hint=rss_pub, metadata=metadata)

        result = ReadyForArticle.try_advance_from(
            pending, body="x" * 100, html_published_at=None
        )

        assert isinstance(result, ReadyForArticle)
        assert result.article.title == "RSS Title"
        assert result.metadata == metadata


class TestFetcherProtocol:
    def test_protocol_declares_provides_classvar(self) -> None:
        """``Fetcher`` が ``PROVIDES: ClassVar[frozenset[str]]`` を宣言する。"""
        # Protocol 上の get_type_hints は ClassVar を unwrap しないため、
        # 生 annotations 文字列で ClassVar / frozenset / str の存在を検証する。
        raw = Fetcher.__annotations__["PROVIDES"]
        as_str = str(raw)
        assert "ClassVar" in as_str
        assert "frozenset" in as_str
        assert "str" in as_str

    def test_concrete_implementation_can_declare_provides(self) -> None:
        """構造的部分型として PROVIDES を宣言できる (Phase 1 ソース実装の最小例)。"""

        class _StubFetcher:
            PROVIDES: ClassVar[frozenset[str]] = frozenset({"summary", "language"})

            def fetch(self, source):  # type: ignore[no-untyped-def]
                raise NotImplementedError

        assert "summary" in _StubFetcher.PROVIDES
        assert _StubFetcher.PROVIDES == frozenset({"summary", "language"})
