"""ReadyForAssessment (Stage 4 precondition 型) のドメインユニットテスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildRejectionReason,
    ReadyForAssessment,
)


def _make_ready(**overrides: object) -> ReadyForAssessment:
    defaults: dict[str, object] = {
        "curation_id": 42,
        "translated_title": "量子コンピューティングの新たなブレイクスルー",
        "summary": "MIT が新手法を発表。量子エラー訂正の分野で大きな進展。",
    }
    defaults.update(overrides)
    return ReadyForAssessment(**defaults)  # type: ignore[arg-type]


class TestReadyForAssessmentImmutability:
    def test_is_frozen(self) -> None:
        ready = _make_ready()
        with pytest.raises(ValidationError):
            ready.curation_id = 999  # type: ignore[misc]

    def test_validates_int_fields(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id="not-an-int",  # type: ignore[arg-type]
                translated_title="t",
                summary="s",
            )

    def test_rejects_non_positive_curation_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForAssessment(
                curation_id=0,
                translated_title="t",
                summary="s",
            )


def test_rejection_reasons_partition_idempotent_skip_from_durable() -> None:
    """処理済みだけを監査対象から除き、欠損と入力不正の理由を記録する。"""
    idempotent = {
        c for c in AssessmentReadyBuildRejectionReason if c.is_idempotent_skip
    }
    durable = {
        c for c in AssessmentReadyBuildRejectionReason if not c.is_idempotent_skip
    }
    assert idempotent == {
        AssessmentReadyBuildRejectionReason.ALREADY_IN_SCOPE,
        AssessmentReadyBuildRejectionReason.ALREADY_OUT_OF_SCOPE,
    }
    assert durable == {
        AssessmentReadyBuildRejectionReason.CURATION_MISSING,
        AssessmentReadyBuildRejectionReason.INPUT_INVALID,
    }
