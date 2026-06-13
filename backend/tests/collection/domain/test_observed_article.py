"""``ObservedArticle`` の業務不変条件テスト (取得済み事実の単一型)。

検証は実装追跡ではなく **JSONB 契約 + strict 性** の不変条件:

1. identity (``source_name`` / ``source_url``) は ``Field(exclude=True)`` で
   JSONB に焼かれない (二重管理排除。表層列 ``source_name`` / ``url`` が唯一の
   authoritative)。in-memory では必須。
2. round-trip 恒等: ``model_dump`` → identity 注入 →
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
    ObservedArticleInvalidError,
    ObservedArticleInvalidReason,
    ObservedField,
    ObservedOrigin,
    _classify_observed_article_error,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.source_name import SourceName

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


def test_model_dump_omits_identity() -> None:
    """``model_dump`` は identity (``source_name`` / ``source_url``) を
    ``exclude=True`` で焼かず、content (title/body/publishedAt) だけを永続化する。
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


def test_try_build_restores_authoritative_identity() -> None:
    """JSONB 退避値に表層 identity を戻して ObservedArticle に復元する。"""
    original = _observed()
    observed_article = original.model_dump(mode="json", by_alias=True)

    restored = ObservedArticle.try_build(
        observed_article=observed_article,
        source_name=SourceName("TechCrunch"),
        source_url=CanonicalArticleUrl(_URL),
    )

    assert restored == original


def test_try_build_raises_domain_error_for_invalid_shape() -> None:
    """復元不能な observed_article は ObservedArticle 側の例外で表す。"""
    with pytest.raises(ObservedArticleInvalidError) as exc_info:
        ObservedArticle.try_build(
            observed_article={"title": {"value": "x", "origin": "invalid"}},
            source_name=SourceName("TechCrunch"),
            source_url=CanonicalArticleUrl(_URL),
        )
    assert exc_info.value.reason is ObservedArticleInvalidReason.TITLE_INVALID


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


class TestObservedArticleInvalidReason:
    """復元失敗を field 単位 reason で分類することの所有テスト。

    reason は ObservedArticle 構築段階だけが loc[0] で掴める PII-free な運用情報。
    """

    @staticmethod
    def _error_for(raw: dict[str, object]) -> ValidationError:
        try:
            ObservedArticle.model_validate(raw)
        except ValidationError as exc:
            return exc
        raise AssertionError("expected ValidationError")

    @pytest.mark.parametrize(
        ("raw", "expected_reason"),
        [
            (
                {"source_url": _URL, "title": {"value": "x", "origin": "feed"}},
                ObservedArticleInvalidReason.SOURCE_NAME_MISSING,
            ),
            (
                {"sourceName": "bad@name!", "source_url": _URL},
                ObservedArticleInvalidReason.SOURCE_NAME_INVALID,
            ),
            (
                {"sourceName": "TechCrunch", "source_url": "ftp://x"},
                ObservedArticleInvalidReason.SOURCE_URL_INVALID,
            ),
            (
                {
                    "sourceName": "TechCrunch",
                    "source_url": _URL,
                    "title": {"value": "x", "origin": "bad"},
                },
                ObservedArticleInvalidReason.TITLE_INVALID,
            ),
            (
                {
                    "sourceName": "TechCrunch",
                    "source_url": _URL,
                    "body": {"value": 123, "origin": "feed"},
                },
                ObservedArticleInvalidReason.BODY_INVALID,
            ),
            (
                {
                    "sourceName": "TechCrunch",
                    "source_url": _URL,
                    "publishedAt": {"value": "not-a-date", "origin": "feed"},
                },
                ObservedArticleInvalidReason.PUBLISHED_AT_INVALID,
            ),
        ],
    )
    def test_classifies_failure_field(
        self, raw: dict[str, object], expected_reason: ObservedArticleInvalidReason
    ) -> None:
        assert _classify_observed_article_error(self._error_for(raw)) is expected_reason

    def test_non_object_observed_article(self) -> None:
        with pytest.raises(ObservedArticleInvalidError) as exc_info:
            ObservedArticle.try_build(
                observed_article=None,  # type: ignore[arg-type]
                source_name=SourceName("TechCrunch"),
                source_url=CanonicalArticleUrl(_URL),
            )
        assert (
            exc_info.value.reason
            is ObservedArticleInvalidReason.OBSERVED_ARTICLE_NOT_OBJECT
        )

    def test_error_message_is_pii_free(self) -> None:
        # input 値 (origin 文字列等) は str(exc) に出ず reason タグのみ
        with pytest.raises(ObservedArticleInvalidError) as exc_info:
            ObservedArticle.try_build(
                observed_article={
                    "title": {"value": "x", "origin": "secret-internal-marker"}
                },
                source_name=SourceName("TechCrunch"),
                source_url=CanonicalArticleUrl(_URL),
            )
        assert "secret-internal-marker" not in str(exc_info.value)
        assert str(exc_info.value) == "observed article input is invalid: title_invalid"
