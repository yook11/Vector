"""``app.observability.domain.payloads`` の単体テスト。

不変条件と field schema を検証する:

- ``SourceFetchPayload``: 5 種 count + ``entry_count == sum(...)`` invariant
- ``ContentFetchPayload``: ``article_url_id`` field と extra="forbid"
"""

from __future__ import annotations

import pytest

from app.observability.domain.payloads import (
    ContentFetchPayload,
    SourceFetchPayload,
)


class TestSourceFetchPayloadInvariant:
    """``entry_count == article_created + completion_queued + skipped + failed``。"""

    def test_default_zero_satisfies_invariant(self) -> None:
        """全 count が 0 (default) の場合は当然 0 == 0 で通る。"""
        payload = SourceFetchPayload()
        assert payload.entry_count == 0
        assert payload.article_created_count == 0
        assert payload.completion_queued_count == 0
        assert payload.skipped_count == 0
        assert payload.failed_count == 0

    def test_balanced_counts_pass(self) -> None:
        """合計が entry_count と一致すれば通る。"""
        payload = SourceFetchPayload(
            entry_count=10,
            article_created_count=4,
            completion_queued_count=3,
            skipped_count=2,
            failed_count=1,
        )
        assert payload.entry_count == 10

    def test_violation_raises_value_error(self) -> None:
        """合計が entry_count と一致しない場合は構築時に ValueError。"""
        with pytest.raises(ValueError, match="entry_count=5"):
            SourceFetchPayload(
                entry_count=5,
                article_created_count=2,
                completion_queued_count=2,
                skipped_count=2,
                failed_count=2,  # sum=8, entry_count=5
            )

    def test_zero_entry_with_nonzero_subtotal_fails(self) -> None:
        """entry_count を 0 のまま、内訳に値を入れると違反。"""
        with pytest.raises(ValueError, match="entry_count=0"):
            SourceFetchPayload(article_created_count=1)

    def test_only_failed_satisfies_with_matching_entry(self) -> None:
        """全エントリ失敗 (Failed のみ) でも entry_count==failed_count なら通る。"""
        payload = SourceFetchPayload(
            entry_count=3,
            failed_count=3,
            failed_codes={"body_too_short": 3},
        )
        assert payload.failed_count == 3


class TestSourceFetchPayloadCodeBreakdowns:
    """sparse breakdown dict (``*_codes``) は None を許容。"""

    def test_skipped_codes_optional(self) -> None:
        payload = SourceFetchPayload(entry_count=2, skipped_count=2)
        assert payload.skipped_codes is None

    def test_completion_reason_codes_optional(self) -> None:
        payload = SourceFetchPayload(
            entry_count=2,
            completion_queued_count=2,
        )
        assert payload.completion_reason_codes is None

    def test_all_breakdowns_can_be_set(self) -> None:
        payload = SourceFetchPayload(
            entry_count=6,
            article_created_count=2,
            completion_queued_count=2,
            skipped_count=1,
            failed_count=1,
            completion_reason_codes={"html_required": 2},
            skipped_codes={"known_url": 1},
            failed_codes={"body_too_short": 1},
        )
        assert payload.completion_reason_codes == {"html_required": 2}
        assert payload.skipped_codes == {"known_url": 1}
        assert payload.failed_codes == {"body_too_short": 1}


class TestContentFetchPayloadArticleUrlId:
    """``ContentFetchPayload.article_url_id`` の field schema 不変条件。"""

    def test_article_url_id_field_exists(self) -> None:
        payload = ContentFetchPayload(article_url_id=42)
        assert payload.article_url_id == 42

    def test_article_url_id_defaults_none(self) -> None:
        payload = ContentFetchPayload()
        assert payload.article_url_id is None

    def test_unknown_field_rejected(self) -> None:
        """未知 field は ``extra="forbid"`` で拒否される。"""
        with pytest.raises(ValueError, match="extra"):
            ContentFetchPayload(unknown_field=42)  # type: ignore[call-arg]


class TestPayloadJsonSerialization:
    """JSONB 焼付経路: ``model_dump(mode='json')`` → Pydantic 再構築の往復。"""

    def test_source_fetch_roundtrip(self) -> None:
        original = SourceFetchPayload(
            fetcher_class="VBFetcher",
            entry_count=3,
            article_created_count=2,
            completion_queued_count=0,
            skipped_count=0,
            failed_count=1,
            failed_codes={"http_403": 1},
        )
        dumped = original.model_dump(mode="json")
        restored = SourceFetchPayload.model_validate(dumped)
        assert restored == original

    def test_content_fetch_roundtrip(self) -> None:
        original = ContentFetchPayload(
            article_url_id=99,
            extractor_class="ArticleHtmlExtractor",
            body_length=12345,
        )
        dumped = original.model_dump(mode="json")
        restored = ContentFetchPayload.model_validate(dumped)
        assert restored == original
