"""Stage 4 (Assessment) ドメインの公開 API。

precondition 型 ``ReadyForAssessment`` と ready build blocked code / exception を
再エクスポートする。
in-scope / out-of-scope の永続化済 Entity は AI 境界 ``InScope`` / ``OutOfScope``
で永続化可能性を保証 → 以降は DB を SSoT として下流が信用する設計に統一したため
廃止 (`feedback_bc_boundary_guarantees_downstream`)。
"""

from __future__ import annotations

from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlockedCode,
    AssessmentReadyBuildBlockedError,
    ReadyForAssessment,
)

__all__ = [
    "AssessmentReadyBuildBlockedCode",
    "AssessmentReadyBuildBlockedError",
    "ReadyForAssessment",
]
