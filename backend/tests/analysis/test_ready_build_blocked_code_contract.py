"""AI Ready build blocked の拒否コード(ログの rejection_code)の契約テスト。"""

from __future__ import annotations

import pytest

from app.analysis.assessment.domain.ready import AssessmentReadyBuildRejectionReason
from app.analysis.curation.domain.ready import CurationReadyBuildRejectionReason
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejectionReason


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        *[("curation", member) for member in CurationReadyBuildRejectionReason],
        *[("assessment", member) for member in AssessmentReadyBuildRejectionReason],
        *[("embedding", member) for member in EmbeddingReadyBuildRejectionReason],
    ],
)
def test_ready_build_blocked_code_value_follows_stage_namespace(
    stage: str,
    member: (
        CurationReadyBuildRejectionReason
        | AssessmentReadyBuildRejectionReason
        | EmbeddingReadyBuildRejectionReason
    ),
) -> None:
    """拒否理由の値は ``{stage}_ready_build_blocked_{name}`` になる。"""
    assert member.value == f"{stage}_ready_build_blocked_{member.name.lower()}"
