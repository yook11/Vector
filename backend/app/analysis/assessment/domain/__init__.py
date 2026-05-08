"""Stage 4 (Assessment) ドメインの公開 API。

Entity (``InScopeAssessment`` / ``OutOfScopeAssessment``) のみを再エクスポートする。
``InScopeAssessmentDraft`` / ``OutOfScopeAssessmentDraft`` は Service / Repository
内限定の過渡型で、外部からは fully-qualified import で参照する。
"""

from __future__ import annotations

from app.analysis.assessment.domain.in_scope import InScopeAssessment
from app.analysis.assessment.domain.out_of_scope import OutOfScopeAssessment

__all__ = ["InScopeAssessment", "OutOfScopeAssessment"]
