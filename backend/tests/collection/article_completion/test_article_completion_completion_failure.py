"""completion concern (Stage 2: 抽出物 + メタデータ合成段) の分類テスト。

acquisition concern (Stage 1) の Retry 軸分類は
``test_article_completion_acquisition_failure.py`` が所有する。本ファイルは Stage 2 の
``ArticleCompletionFailure`` を Accept 軸の ``CompletionRejection`` に正規化する
``classify_article_completion_failure`` の契約のみを検証する。

不変条件:
- ``reason_code`` は ``completion_*`` prefix の audit 集計 key として variant 毎に安定。
- variant 固有の証拠 (例外 class+message) が ``detail`` に畳まれる。証拠を持たない
  variant では ``detail`` は ``None``。
"""

from __future__ import annotations

from app.collection.article_completion.completion_failure import (
    CompletionInvariantRejected,
    CompletionRejection,
    PublishedAtMissing,
    classify_article_completion_failure,
)


def test_published_at_missing_maps_to_stable_reason_without_detail() -> None:
    """``PublishedAtMissing`` → ``completion_published_at_missing`` (証拠なし)。

    観測点 (observed/html の在/不在) は audit ラベルには畳まず ``detail`` は None。
    """
    failure = PublishedAtMissing(observed_had_value=False, html_had_value=True)

    assert classify_article_completion_failure(failure) == CompletionRejection(
        reason_code="completion_published_at_missing"
    )


def test_invariant_rejected_folds_exception_evidence_into_detail() -> None:
    """``CompletionInvariantRejected`` → ``completion_invariant_rejected`` + 例外証拠。

    ``detail`` は variant の error_class / error_message を ``"{ec}: {em}"`` で畳む。
    """
    failure = CompletionInvariantRejected(
        error_class="ValueError", error_message="boom"
    )

    assert classify_article_completion_failure(failure) == CompletionRejection(
        reason_code="completion_invariant_rejected",
        detail="ValueError: boom",
    )


def test_reason_code_distinguishes_variants() -> None:
    """2 variant は別 ``reason_code`` に分かれる (audit 集計 key の弁別性)。"""
    published_at_missing = classify_article_completion_failure(
        PublishedAtMissing(observed_had_value=False, html_had_value=False)
    )
    invariant_rejected = classify_article_completion_failure(
        CompletionInvariantRejected(error_class="ValueError", error_message="x")
    )

    assert published_at_missing.reason_code != invariant_rejected.reason_code
