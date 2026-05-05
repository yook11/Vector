"""``StagedArticleAttributes`` Pydantic constraint の不変条件テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.collection.ingestion.domain.staged_attributes import StagedArticleAttributes


class TestStagedArticleAttributesContract:
    def test_default_values_are_none(self) -> None:
        # 全 field が optional + default=None
        attrs = StagedArticleAttributes()
        assert attrs.title is None
        assert attrs.published_at is None

    def test_accepts_partial_fields(self) -> None:
        # title だけ、published_at だけといった partial 構築を許容
        only_title = StagedArticleAttributes(title="Hello")
        assert only_title.title == "Hello"
        assert only_title.published_at is None

        only_pubdate = StagedArticleAttributes(
            published_at=datetime(2026, 5, 6, tzinfo=UTC)
        )
        assert only_pubdate.title is None
        assert only_pubdate.published_at == datetime(2026, 5, 6, tzinfo=UTC)

    def test_accepts_all_fields(self) -> None:
        attrs = StagedArticleAttributes(
            title="Article Title",
            published_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        assert attrs.title == "Article Title"
        assert attrs.published_at == datetime(2026, 5, 6, 12, 0, tzinfo=UTC)

    def test_extra_field_is_forbidden(self) -> None:
        # 未定義 field は writer の typo を疑い拒否する
        with pytest.raises(ValidationError) as exc_info:
            StagedArticleAttributes(title="t", unknown_field="x")  # type: ignore[call-arg]
        # extra=forbid に由来するエラーであることを確認
        assert any(err["type"] == "extra_forbidden" for err in exc_info.value.errors())

    def test_frozen_prevents_mutation(self) -> None:
        # JSONB に焼き付けた後の改変を防ぐため immutable
        attrs = StagedArticleAttributes(title="Original")
        with pytest.raises(ValidationError):
            attrs.title = "Mutated"  # type: ignore[misc]

    def test_round_trip_via_json(self) -> None:
        # JSONB 永続化を想定した serialize/deserialize の対称性
        original = StagedArticleAttributes(
            title="Round Trip",
            published_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        json_str = original.model_dump_json()
        restored = StagedArticleAttributes.model_validate_json(json_str)
        assert restored == original
