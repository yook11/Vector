"""Stage 4 (Assessment) ドメインの公開 API。

Entity (``InScopeAssessment`` / ``OutOfScopeAssessment``) を再エクスポートする。
"""

from __future__ import annotations

from app.analysis.assessment.domain.in_scope import InScopeAssessment
from app.analysis.assessment.domain.out_of_scope import OutOfScopeAssessment

__all__ = ["InScopeAssessment", "OutOfScopeAssessment"]
