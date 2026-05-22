"""``AnalyzableArticle.build_or_reject`` の業務不変条件テスト。

route 2 (完成段) 用 smart constructor の正本テスト。``try_build`` (route 1,
``Self | None``) と異なり、構築不能の理由を ``QualityTooLow`` の証拠として返す。
ここが「構築不変条件違反は証拠付きで失敗値化する」の正本 (completer の翻訳は
副次)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.collection.domain.analyzable_article import (
    AnalyzableArticle,
    QualityTooLow,
)
from app.collection.domain.article_limits import ARTICLE_BODY_MIN_LENGTH
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.value_objects import PublishedAt

_URL = CanonicalArticleUrl("https://example.com/article")
_PUB = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))
_VALID_TITLE = "Valid Title"
_VALID_BODY = "x" * (ARTICLE_BODY_MIN_LENGTH + 10)
# body 下限を 1 文字下回る = 構築不能の境界 (閾値は production 定数から導出)
_SHORT_BODY = "x" * (ARTICLE_BODY_MIN_LENGTH - 1)


def _build(*, title: str | None = _VALID_TITLE, body: str | None = _VALID_BODY):
    return AnalyzableArticle.build_or_reject(
        title=title,
        body=body,
        published_at=_PUB,
        source_id=1,
        source_url=_URL,
    )


def test_build_or_reject_returns_article_when_invariants_met() -> None:
    """全不変条件を満たす材料は ``AnalyzableArticle`` を構築して返す。"""
    assert isinstance(_build(), AnalyzableArticle)


def test_build_or_reject_returns_quality_too_low_when_body_below_min() -> None:
    """body が下限未満なら構築せず ``QualityTooLow`` を値で返す。"""
    assert isinstance(_build(body=_SHORT_BODY), QualityTooLow)


def test_quality_too_low_carries_validation_evidence() -> None:
    """``QualityTooLow`` は拒否した ``ValidationError`` の証拠を保持する。"""
    result = _build(body=_SHORT_BODY)
    assert isinstance(result, QualityTooLow)
    assert result.error_class == "ValidationError"
    assert "body" in result.error_message
