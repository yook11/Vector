"""Evidence Reviewerのtimeoutと失敗理由に使う定数。"""

from __future__ import annotations

__all__ = [
    "EVIDENCE_REVIEW_TIMEOUT_SECONDS",
    "REVIEWER_ERROR_REASON",
    "REVIEWER_TIMEOUT_REASON",
]

EVIDENCE_REVIEW_TIMEOUT_SECONDS = 30
REVIEWER_TIMEOUT_REASON = "reviewer_timeout"
REVIEWER_ERROR_REASON = "reviewer_error"
