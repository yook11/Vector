"""completion concern (Stage 2: 抽出物 + メタデータ合成段) の翻訳テスト。

acquisition concern (Stage 1) の Retry 軸分類は
``test_article_completion_acquisition_failure.py`` が所有する。本ファイルは Stage 2 の
domain 失敗 ``QualityTooLow`` を Accept 軸の ``CompletionRejection`` に畳む
``CompletionRejection.from_quality_too_low`` と、``detail`` の上限 truncation の
契約のみを検証する。

不変条件:
- ``reason_code`` は ``completion_*`` prefix の audit 集計 key として安定。
- 例外固有の証拠 (class+message) が ``detail`` に畳まれる。
- ``detail`` は 500 字でキャップされる (audit log field の upper bound)。
"""

from __future__ import annotations

from app.collection.article_completion.completion_failure import (
    _ERROR_MESSAGE_MAX,
    CompletionRejection,
)
from app.collection.domain.analyzable_article import QualityTooLow


def test_from_quality_too_low_folds_exception_evidence_into_detail() -> None:
    """``QualityTooLow`` → ``completion_invariant_rejected`` + 例外証拠。

    ``detail`` は error_class / error_message を ``"{ec}: {em}"`` で畳む。
    """
    quality = QualityTooLow(error_class="ValueError", error_message="boom")

    assert CompletionRejection.from_quality_too_low(quality) == CompletionRejection(
        reason_code="completion_invariant_rejected",
        detail="ValueError: boom",
    )


def test_detail_truncated_to_upper_bound() -> None:
    """``detail`` が上限超のとき ``_ERROR_MESSAGE_MAX`` 字に切られる。

    Pydantic の冗長な ValidationError message が audit log を溢れさせないための
    upper bound。境界を非空虚に検出するため上限超の長さを入力する。
    """
    oversized = "x" * (_ERROR_MESSAGE_MAX + 50)

    rejection = CompletionRejection(
        reason_code="completion_invariant_rejected", detail=oversized
    )

    assert len(rejection.detail) == _ERROR_MESSAGE_MAX
