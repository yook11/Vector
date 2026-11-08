"""LLM 失敗。想定できる失敗は CODE、想定外は unclassified。"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "UNCLASSIFIED_FAILURE_CODE",
    "LlmAttemptFailed",
    "llm_attempt_failed_from",
]

UNCLASSIFIED_FAILURE_CODE = "unclassified"


@dataclass(frozen=True, slots=True)
class LlmAttemptFailed:
    """失敗で終了した provider attempt。"""

    failure_code: str

    def __post_init__(self) -> None:
        if not self.failure_code:
            raise ValueError("failure_code must not be empty")


def llm_attempt_failed_from(error: BaseException) -> LlmAttemptFailed:
    """CODE から failure_code を決める。クラス名へは落とさない。"""

    code = getattr(error, "CODE", None)
    if isinstance(code, str) and code:
        return LlmAttemptFailed(failure_code=code)
    raise ValueError("classified failure requires CODE")
