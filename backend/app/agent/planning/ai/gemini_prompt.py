"""Gemini question planner prompt renderer."""

from __future__ import annotations

from typing import ClassVar

from app.agent.planning.ai.prompts import (
    QUESTION_PLANNER_PROMPT,
    QUESTION_PLANNER_REPAIR_PROMPT,
)
from app.agent.planning.contract import PlanningRequest
from app.analysis.prompt_safety import sanitize_for_untrusted_block


class GeminiQuestionPlannerPrompt:
    """Question planner prompt for Gemini."""

    TEMPLATE: ClassVar[str] = QUESTION_PLANNER_PROMPT

    @classmethod
    def render(
        cls,
        *,
        request: PlanningRequest,
        previous_error: str | None = None,
    ) -> str:
        prompt = cls.TEMPLATE.format(
            question=sanitize_for_untrusted_block(request.context.standalone_question),
            as_of=request.as_of.isoformat(),
            content_requirements=_render_requirements(
                request.context.content_requirements
            ),
            response_requirements=_render_requirements(
                request.context.response_requirements
            ),
            relevant_prior_coverage=sanitize_for_untrusted_block(
                request.context.relevant_prior_coverage
            ),
            active_goal=sanitize_for_untrusted_block(request.context.active_goal),
            format_only_retrieval_rule=(
                "形式・文体・簡潔さだけを理由に retrieval を増やさない"
            ),
        )
        if previous_error is None:
            return prompt
        return prompt + QUESTION_PLANNER_REPAIR_PROMPT.format(
            previous_error=sanitize_for_untrusted_block(previous_error)
        )


def _render_requirements(requirements: list[object]) -> str:
    return "\n".join(
        "\n".join(
            [
                "<untrusted_input>",
                f"{getattr(requirement, 'requirement_id')}: "
                f"{sanitize_for_untrusted_block(getattr(requirement, 'description'))}",
                "</untrusted_input>",
            ]
        )
        for requirement in requirements
    )
