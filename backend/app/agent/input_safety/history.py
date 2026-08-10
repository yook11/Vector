"""会話履歴からinput safety検査の直前turnを射影する。"""

from __future__ import annotations

from app.agent.input_safety.contract import (
    INPUT_SAFETY_TEXT_CHAR_CAP,
    InputSafetyPreviousTurn,
)
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = ["previous_turn_from_history"]


def previous_turn_from_history(
    history: tuple[ThreadMessageSnapshot, ...],
) -> InputSafetyPreviousTurn | None:
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if message.role != "user":
            continue
        assistant_answer: str | None = None
        if index + 1 < len(history):
            next_message = history[index + 1]
            if next_message.role == "assistant" and next_message.content:
                assistant_answer = next_message.content[:INPUT_SAFETY_TEXT_CHAR_CAP]
        return InputSafetyPreviousTurn(
            user_question=message.content[:INPUT_SAFETY_TEXT_CHAR_CAP],
            assistant_answer=assistant_answer,
        )
    return None
