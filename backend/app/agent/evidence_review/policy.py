"""Evidence Reviewerのtimeoutと失敗分類に関する純粋な規則。"""

from __future__ import annotations

__all__ = [
    "EVIDENCE_REVIEW_TIMEOUT_SECONDS",
    "REVIEWER_ERROR_REASON",
    "REVIEWER_TIMEOUT_REASON",
    "resolve_reviewer_failure_reason",
]

EVIDENCE_REVIEW_TIMEOUT_SECONDS = 30
REVIEWER_TIMEOUT_REASON = "reviewer_timeout"
REVIEWER_ERROR_REASON = "reviewer_error"


def resolve_reviewer_failure_reason(
    *,
    reason: str | None,
    code: str | None,
) -> str:
    if reason is not None:
        return reason
    if code is not None:
        return code
    return REVIEWER_ERROR_REASON
