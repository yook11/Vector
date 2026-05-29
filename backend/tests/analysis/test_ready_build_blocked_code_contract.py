"""AI Ready build blocked outcome_code の契約テスト。"""

from __future__ import annotations

import pytest

from app.analysis.assessment.domain.ready import AssessmentReadyBuildBlockedCode
from app.analysis.curation.domain.ready import CurationReadyBuildBlockedCode
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildBlockedCode


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        *[("curation", member) for member in CurationReadyBuildBlockedCode],
        *[("assessment", member) for member in AssessmentReadyBuildBlockedCode],
        *[("embedding", member) for member in EmbeddingReadyBuildBlockedCode],
    ],
)
def test_ready_build_blocked_code_value_is_audit_outcome_code(
    stage: str,
    member: (
        CurationReadyBuildBlockedCode
        | AssessmentReadyBuildBlockedCode
        | EmbeddingReadyBuildBlockedCode
    ),
) -> None:
    assert member.value == f"{stage}_ready_build_blocked_{member.name.lower()}"
