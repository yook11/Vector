"""Gemini question planner prompt renderer."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from app.agent.planning.ai.prompts import (
    QUESTION_PLANNER_PROMPT,
    QUESTION_PLANNER_REPAIR_PROMPT,
)
from app.analysis.prompt_safety import sanitize_for_untrusted_block


class GeminiQuestionPlannerPrompt:
    """Question planner prompt for Gemini."""

    TEMPLATE: ClassVar[str] = QUESTION_PLANNER_PROMPT

    @classmethod
    def render(
        cls,
        *,
        question: str,
        as_of: datetime,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
        previous_error: str | None = None,
    ) -> str:
        prompt = cls.TEMPLATE.format(
            question=sanitize_for_untrusted_block(question),
            as_of=as_of.isoformat(),
            user_intent=sanitize_for_untrusted_block(user_intent),
            prior_coverage=sanitize_for_untrusted_block(prior_coverage),
            user_activity_context=sanitize_for_untrusted_block(user_activity_context),
        )
        if previous_error is None:
            return prompt
        return prompt + QUESTION_PLANNER_REPAIR_PROMPT.format(
            previous_error=sanitize_for_untrusted_block(previous_error)
        )
