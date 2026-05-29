"""``AnalyzableArticle.build_or_reject`` の業務不変条件テスト。

route 2 (完成段) 用 smart constructor の正本テスト。``try_build`` (route 1,
``Self | None``) と異なり、構築不能の理由を ``QualityTooLow`` の defect 集合として
返す。ここが「構築不変条件違反は分類済み defect で失敗値化する」の正本 (completer の
翻訳は副次)。

写像 totality は **入力駆動** で守る: 各違反入力 → 期待 defect tuple を固定し、
``build_or_reject`` を公開 API として直接呼ぶ (内部の写像 dict を覗かない)。
新しい不変条件追加 / Field 型変更 (例: PublishedAt の型変更で dataclass_type が
消える) はこのオラクルが赤で捕捉する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.domain.analyzable_article import (
    AnalyzableArticle,
    AnalyzableArticleDefect,
    QualityTooLow,
)
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.value_objects import PublishedAt

_URL = CanonicalArticleUrl("https://example.com/article")
_PUB = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))
_VALID_TITLE = "Valid Title"
_VALID_BODY = "x" * (ARTICLE_BODY_MIN_LENGTH + 10)


def _build(
    *,
    title: str | None = _VALID_TITLE,
    body: str | None = _VALID_BODY,
    published_at: PublishedAt | None = _PUB,
    source_id: int = 1,
):
    return AnalyzableArticle.build_or_reject(
        title=title,
        body=body,
        published_at=published_at,
        source_id=source_id,
        source_url=_URL,
    )


def test_build_or_reject_returns_article_when_invariants_met() -> None:
    """全不変条件を満たす材料は ``AnalyzableArticle`` を構築して返す。"""
    assert isinstance(_build(), AnalyzableArticle)


# 各違反入力 → 期待 defect。閾値は production 定数から導出 (テストに複製しない)。
_SINGLE_DEFECT_CASES = [
    ("title_none", {"title": None}, AnalyzableArticleDefect.TITLE_MISSING),
    ("title_empty", {"title": ""}, AnalyzableArticleDefect.TITLE_TOO_SHORT),
    (
        "title_too_long",
        {"title": "x" * (ARTICLE_TITLE_MAX_LENGTH + 1)},
        AnalyzableArticleDefect.TITLE_TOO_LONG,
    ),
    ("body_none", {"body": None}, AnalyzableArticleDefect.BODY_MISSING),
    (
        "body_too_short",
        {"body": "x" * (ARTICLE_BODY_MIN_LENGTH - 1)},
        AnalyzableArticleDefect.BODY_TOO_SHORT,
    ),
    (
        "body_too_long",
        {"body": "x" * (ARTICLE_BODY_MAX_LENGTH + 1)},
        AnalyzableArticleDefect.BODY_TOO_LONG,
    ),
    (
        "published_at_none",
        {"published_at": None},
        AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
    ),
    ("source_id_zero", {"source_id": 0}, AnalyzableArticleDefect.SOURCE_ID_INVALID),
    (
        "source_id_negative",
        {"source_id": -5},
        AnalyzableArticleDefect.SOURCE_ID_INVALID,
    ),
]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [(case[1], case[2]) for case in _SINGLE_DEFECT_CASES],
    ids=[case[0] for case in _SINGLE_DEFECT_CASES],
)
def test_build_or_reject_classifies_single_invariant_violation(
    overrides: dict[str, object],
    expected: AnalyzableArticleDefect,
) -> None:
    """単一不変条件違反は対応する defect 1 件に分類される (写像 totality)。"""
    result = _build(**overrides)  # type: ignore[arg-type]
    assert isinstance(result, QualityTooLow)
    assert result.defects == (expected,)


def test_build_or_reject_collects_multiple_defects_in_field_order() -> None:
    """複数違反は Field 宣言順 (= errors() 順) で全件 defect に集約される。

    主 defect は先頭 = body (published_at より前の Field) になる。
    """
    result = _build(body=None, published_at=None)
    assert isinstance(result, QualityTooLow)
    assert result.defects == (
        AnalyzableArticleDefect.BODY_MISSING,
        AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
    )


def test_build_or_reject_records_no_unmapped_for_known_violations() -> None:
    """既知の不変条件違反は写像に乗り、``unmapped`` は空のまま。"""
    result = _build(body=None)
    assert isinstance(result, QualityTooLow)
    assert result.unmapped == ()
