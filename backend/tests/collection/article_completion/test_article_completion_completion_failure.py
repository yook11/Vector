"""completion concern (Stage 2: 抽出物 + メタデータ合成段) の翻訳テスト。

scrape concern (Stage 1) の Retry 軸分類は
``test_article_completion_scrape_failure.py`` が所有する。本ファイルは Stage 2 の
domain 失敗 ``QualityTooLow`` を Accept 軸の ``CompletionRejection`` に畳む
``CompletionRejection.from_quality_too_low`` と、defect 集合の運搬契約のみを検証する。

不変条件:
- ドメインの分類済み defect 集合を無加工で運ぶ (翻訳で語彙を作り直さない)。
- ``reason_code`` = 主 defect (= ``defects[0]``) value = audit outcome_code。
- ``defect_codes`` = 全 defect の value tuple = audit payload.defects。
- 写像漏れ痕跡 ``unmapped`` もそのまま運ぶ。
"""

from __future__ import annotations

from app.collection.article_completion.completion_failure import CompletionRejection
from app.collection.domain.analyzable_article import (
    AnalyzableArticleDefect,
    QualityTooLow,
)


def test_from_quality_too_low_carries_defects_unchanged() -> None:
    """``QualityTooLow`` の defect 集合を無加工で ``CompletionRejection`` に運ぶ。"""
    quality = QualityTooLow(
        defects=(
            AnalyzableArticleDefect.BODY_MISSING,
            AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
        )
    )

    assert CompletionRejection.from_quality_too_low(quality) == CompletionRejection(
        defects=(
            AnalyzableArticleDefect.BODY_MISSING,
            AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
        )
    )


def test_reason_code_is_primary_defect_value() -> None:
    """``reason_code`` は先頭 defect の value (= audit outcome_code)。"""
    rejection = CompletionRejection(
        defects=(
            AnalyzableArticleDefect.BODY_MISSING,
            AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
        )
    )

    assert rejection.reason_code == "analyzable_article_body_missing"


def test_defect_codes_expose_full_set_in_order() -> None:
    """``defect_codes`` は全 defect の value を順序保存で返す (= payload.defects)。"""
    rejection = CompletionRejection(
        defects=(
            AnalyzableArticleDefect.BODY_MISSING,
            AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
        )
    )

    assert rejection.defect_codes == (
        "analyzable_article_body_missing",
        "analyzable_article_published_at_missing",
    )


def test_from_quality_too_low_carries_unmapped_trace() -> None:
    """写像漏れ痕跡 ``unmapped`` も無加工で運ぶ。"""
    quality = QualityTooLow(
        defects=(AnalyzableArticleDefect.UNMAPPED_VALIDATION_ERROR,),
        unmapped=("title:some_new_constraint",),
    )

    assert CompletionRejection.from_quality_too_low(quality).unmapped == (
        "title:some_new_constraint",
    )
