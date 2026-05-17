"""``ObservedArticle`` の業務不変条件テスト (取得済み事実の単一型)。

検証は実装追跡ではなく **JSONB 契約 + 後方互換 + strict 性** の不変条件:

1. ``source_url`` は ``Field(exclude=True)`` で JSONB に焼かれない
   (二重管理排除。``url`` 列が唯一の authoritative)。in-memory では必須。
2. round-trip 恒等: ``model_dump(by_alias=True)`` → identity 注入 →
   ``model_validate`` で同値復元 (Stage1 enqueue → Stage2 hydrate)。
3. 後方互換: 旧 ``StagedArticleAttributes`` 形 (schemaVersion 不在) を
   before-validator が **shape のみ** 変換 (title/published_at 設定 /
   body 不在 / prefer_html_title 破棄)。DB は触らない。
4. strict 性: identity (sourceName) 欠落 raw は ``ValidationError``
   (Optional identity を持たない = ACL が必ず注入する契約)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.source_name import SourceName

_URL = "https://example.com/p/observed"
_PUB = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))


def _observed() -> ObservedArticle:
    return ObservedArticle(
        source_name=SourceName("TechCrunch"),
        source_url=CanonicalArticleUrl(_URL),
        title=ObservedField(value="T", origin=ObservedOrigin.feed),
        body=None,
        published_at=ObservedField(value=_PUB, origin=ObservedOrigin.sitemap),
    )


def test_source_url_is_excluded_from_jsonb_dump() -> None:
    """``source_url`` は型レベルで永続化対象外 (drift 排除)。"""
    dumped = _observed().model_dump(mode="json", by_alias=True)
    assert "source_url" not in dumped
    assert "sourceUrl" not in dumped
    assert dumped["sourceName"] == "TechCrunch"
    assert set(dumped) == {
        "schemaVersion",
        "sourceName",
        "title",
        "body",
        "publishedAt",
    }


def test_round_trip_identity_with_acl_injected_source_url() -> None:
    """dump → (ACL が url 列から source_url 注入) → validate で同値復元。"""
    original = _observed()
    raw = original.model_dump(mode="json", by_alias=True)
    raw["source_url"] = _URL  # repository(ACL) が url 列から注入する責務
    restored = ObservedArticle.model_validate(raw)
    assert restored == original
    assert restored.published_at is not None
    assert restored.published_at.origin is ObservedOrigin.sitemap


def test_legacy_shape_is_absorbed_to_observed_shape() -> None:
    """旧 ``StagedArticleAttributes`` 形を before-validator が変換する。

    title→title{value,origin:feed} / published_at_hint→publishedAt /
    body 不在 / prefer_html_title 破棄。identity は ACL が事前注入。
    """
    legacy = {
        "title": "Legacy Title",
        "published_at_hint": {"value": "2026-05-01T00:00:00Z"},
        "prefer_html_title": True,
        "sourceName": "Anthropic",  # ACL が legacy raw へ事前注入
        "source_url": _URL,
    }
    observed = ObservedArticle.model_validate(legacy)
    assert observed.title is not None
    assert observed.title.value == "Legacy Title"
    assert observed.title.origin is ObservedOrigin.feed
    assert observed.body is None  # 旧形は body を持たない
    assert observed.published_at is not None
    assert observed.published_at.value == PublishedAt(
        value=datetime(2026, 5, 1, tzinfo=UTC)
    )
    assert observed.source_name == SourceName("Anthropic")
    assert not hasattr(observed, "prefer_html_title")  # policy は profile 所有


def test_missing_identity_is_strict_validation_error() -> None:
    """sourceName 欠落 raw は ``ValidationError`` (Optional identity なし)。"""
    with pytest.raises(ValidationError):
        ObservedArticle.model_validate(
            {
                "schemaVersion": 1,
                "source_url": _URL,
                "title": {"value": "x", "origin": "feed"},
            }
        )


def test_frozen_instance_rejects_mutation() -> None:
    """生成後は不変 (観測事実 VO)。"""
    observed = _observed()
    with pytest.raises(ValidationError):
        observed.source_name = SourceName("Other")  # type: ignore[misc]
