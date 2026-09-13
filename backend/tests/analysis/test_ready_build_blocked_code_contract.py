"""AI Ready build blocked outcome_code の契約テスト。"""

from __future__ import annotations

import pytest

from app.analysis.assessment.domain.ready import AssessmentReadyBuildRejectionReason
from app.analysis.curation.domain.ready import CurationReadyBuildBlockedCode
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejectionReason


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        *[("curation", member) for member in CurationReadyBuildBlockedCode],
        *[("assessment", member) for member in AssessmentReadyBuildRejectionReason],
        *[("embedding", member) for member in EmbeddingReadyBuildRejectionReason],
    ],
)
def test_ready_build_blocked_code_value_is_audit_outcome_code(
    stage: str,
    member: (
        CurationReadyBuildBlockedCode
        | AssessmentReadyBuildRejectionReason
        | EmbeddingReadyBuildRejectionReason
    ),
) -> None:
    assert member.value == f"{stage}_ready_build_blocked_{member.name.lower()}"
