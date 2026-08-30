"""分類済み LLM 失敗を Recorder 結論へ写す。"""

from __future__ import annotations

from app.agent.error_type import span_error_type
from app.agent.recording.llm import LlmAttemptFailed

__all__ = ["llm_attempt_failed_from"]


def llm_attempt_failed_from(error: BaseException) -> LlmAttemptFailed:
    """span の error.type と同じ語彙で failure_code を決める。"""

    return LlmAttemptFailed(failure_code=span_error_type(error))
