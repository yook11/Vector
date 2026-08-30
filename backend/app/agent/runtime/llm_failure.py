"""分類済み LLM 失敗。起こった側が持ち、code で何が起きたかを名乗る。"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.error_type import span_error_type

__all__ = ["LlmAttemptFailed", "llm_attempt_failed_from"]


@dataclass(frozen=True, slots=True)
class LlmAttemptFailed:
    """分類済み失敗で終了した provider attempt。"""

    failure_code: str

    def __post_init__(self) -> None:
        if not self.failure_code:
            raise ValueError("failure_code must not be empty")


def llm_attempt_failed_from(error: BaseException) -> LlmAttemptFailed:
    """span の error.type と同じ語彙で failure_code を決める。"""

    return LlmAttemptFailed(failure_code=span_error_type(error))
