"""completion concern (Stage 2: 抽出物 + メタデータ合成段) の分類テスト。

acquisition concern (Stage 1) の Retry 軸分類は
``test_article_completion_acquisition_failure.py`` が所有する。本ファイルは Stage 2 の
``CompletionInvariantRejected`` を Accept 軸の ``CompletionRejection`` に正規化する
``classify_article_completion_failure`` の契約のみを検証する。

不変条件:
- ``reason_code`` は ``completion_*`` prefix の audit 集計 key として安定。
- 例外固有の証拠 (class+message) が ``detail`` に畳まれる。
"""

from __future__ import annotations

from app.collection.article_completion.completion_failure import (
    CompletionInvariantRejected,
    CompletionRejection,
    classify_article_completion_failure,
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
