"""Stage 4 (Assessment) ドメインの公開 API。

precondition 型 ``ReadyForAssessment`` と Ready構築の拒否理由・結果 を
再エクスポートする。AI 境界で永続化可能性を保証し、以降は DB を SSoT として
下流が信用する。
"""

from __future__ import annotations

from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildRejected,
    AssessmentReadyBuildRejectionReason,
    ReadyForAssessment,
)

__all__ = [
    "AssessmentReadyBuildRejectionReason",
    "AssessmentReadyBuildRejected",
    "ReadyForAssessment",
]
