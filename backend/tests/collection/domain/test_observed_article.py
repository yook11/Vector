"""``ObservedArticle`` の業務不変条件テスト (取得済み事実の単一型)。

検証は実装追跡ではなく **JSONB 契約 + strict 性** の不変条件:

1. identity (``source_name`` / ``source_url``) は ``Field(exclude=True)`` で
   JSONB に焼かれない (二重管理排除。表層列 ``source_name`` / ``url`` が唯一の
   authoritative)。in-memory では必須。
2. round-trip 恒等: ``model_dump(by_alias=True)`` → identity 注入 →
   ``model_validate`` で同値復元 (Stage1 enqueue → Stage2 hydrate)。
3. strict 性: identity (sourceName) 欠落 raw は ``ValidationError``
   (Optional identity を持たない = ACL が必ず注入する契約)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
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


def test_identity_is_excluded_from_jsonb_dump() -> None:
    """identity (``source_name`` / ``source_url``) は ``Field(exclude=True)``
    で永続化対象外 (drift 排除)。表層列が SSoT で JSONB は事実だけを焼く。
    """
    dumped = _observed().model_dump(mode="json", by_alias=True)
    assert "source_url" not in dumped
    assert "sourceUrl" not in dumped
    assert "source_name" not in dumped
    assert "sourceName" not in dumped
    assert set(dumped) == {"title", "body", "publishedAt"}


def test_round_trip_identity_with_acl_injected_identity() -> None:
    """dump → (ACL が表層列から identity 注入) → validate で同値復元。

    ``source_name`` / ``source_url`` のどちらも ``exclude=True`` で dump に
    出ないため、repository(ACL) が表層列 ``source_name`` / ``url`` から
    raw に注入する責務を負う。本 test は wire 契約をその責務込みで pin する。
    """
    original = _observed()
    raw = original.model_dump(mode="json", by_alias=True)
    raw["sourceName"] = "TechCrunch"  # repository が source_name 列から注入
    raw["source_url"] = _URL  # repository が url 列から注入
    restored = ObservedArticle.model_validate(raw)
    assert restored == original
    assert restored.published_at is not None
    assert restored.published_at.origin is ObservedOrigin.sitemap


def test_missing_identity_is_strict_validation_error() -> None:
    """sourceName 欠落 raw は ``ValidationError`` (Optional identity なし)。"""
    with pytest.raises(ValidationError):
        ObservedArticle.model_validate(
            {
                "source_url": _URL,
                "title": {"value": "x", "origin": "feed"},
            }
        )


def test_frozen_instance_rejects_mutation() -> None:
    """生成後は不変 (観測事実 VO)。"""
    observed = _observed()
    with pytest.raises(ValidationError):
        observed.source_name = SourceName("Other")  # type: ignore[misc]
