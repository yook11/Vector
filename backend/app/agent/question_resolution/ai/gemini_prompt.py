"""Gemini question-resolution prompt renderer."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from app.agent.question_resolution.ai.prompts import QUESTION_RESOLUTION_PROMPT
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.prompt_safety import sanitize_for_untrusted_block


class GeminiQuestionResolutionPrompt:
    """Prompt renderer that preserves every conversation field as untrusted text."""

    TEMPLATE: ClassVar[str] = QUESTION_RESOLUTION_PROMPT

    @classmethod
    def render(
        cls,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> str:
        return cls.TEMPLATE.format(
            question=sanitize_for_untrusted_block(question),
            history=_render_history(history),
            as_of=as_of.isoformat(),
        )


def _render_history(history: list[ThreadMessageSnapshot]) -> str:
    return "\\n\\n".join(
        "\\n".join(
            [
                f"role: {message.role}",
                "<untrusted_input>",
                sanitize_for_untrusted_block(message.content),
                "</untrusted_input>",
            ]
        )
        for message in history
    )
