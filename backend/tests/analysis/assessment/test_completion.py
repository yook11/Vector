"""正常終了と保存済み記事IDの組み合わせを検証する。"""

import pytest

from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)


@pytest.mark.parametrize("article_id", [None, 0, -1, True, False, 1.0, "1"])
def test_in_scope_requires_positive_integer_article_id(article_id: object) -> None:
    """対象内保存の正常終了に、欠落・不正な記事IDを持たせない。"""
    with pytest.raises(ValueError, match="positive integer"):
        AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, article_id)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kind",
    [AssessmentCompletionKind.OUT_OF_SCOPE, AssessmentCompletionKind.ALREADY_ASSESSED],
)
def test_non_in_scope_completion_rejects_article_id(
    kind: AssessmentCompletionKind,
) -> None:
    """新規の対象内保存以外の終了に後続投入用IDを持たせない。"""
    with pytest.raises(ValueError, match="only in_scope"):
        AssessmentCompletion(kind, 1)


@pytest.mark.parametrize("kind", [None, "in_scope", "failed"])
def test_completion_requires_declared_kind(kind: object) -> None:
    """自由文字列や失敗を正常終了の種類として受け付けない。"""
    with pytest.raises(TypeError, match="AssessmentCompletionKind"):
        AssessmentCompletion(kind)  # type: ignore[arg-type]
