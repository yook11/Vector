"""保存完了と処理済みを区別するCurationCompletionの契約。"""

from dataclasses import FrozenInstanceError

import pytest

from app.analysis.curation.service import CurationCompletion, CurationCompletionKind


@pytest.mark.parametrize("curation_id", [None, 0, -1, True, 1.0, "1"])
def test_signal_requires_positive_integer_id(curation_id):
    """Signalの保存完了には正の整数IDを必須とする。"""
    with pytest.raises(ValueError):
        CurationCompletion(CurationCompletionKind.SIGNAL, curation_id)


@pytest.mark.parametrize(
    "kind", [CurationCompletionKind.NOISE, CurationCompletionKind.ALREADY_CURATED]
)
def test_only_signal_carries_id(kind):
    """Noiseと処理済みにはSignalの保存IDを付与できない。"""
    assert CurationCompletion(kind).curation_id is None
    with pytest.raises(ValueError):
        CurationCompletion(kind, 42)


def test_completion_rejects_untyped_kind():
    """列挙型以外を正常終了の種類として受け付けない。"""
    with pytest.raises(TypeError):
        CurationCompletion("signal", 42)


def test_saved_signal_completion_is_immutable():
    """保存済みIDを持つSignalの完了結果を後から変更できない。"""
    completion = CurationCompletion(CurationCompletionKind.SIGNAL, 42)
    assert completion.curation_id == 42
    with pytest.raises(FrozenInstanceError):
        completion.curation_id = 99
