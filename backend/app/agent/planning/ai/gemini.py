"""Gemini implementation of the question planner."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Final

from google import genai
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.agent.contract import AnswerQuestionInput
from app.agent.planning.ai.gemini_prompt import GeminiQuestionPlannerPrompt
from app.agent.planning.ai.gemini_spec import (
    GEMINI_QUESTION_PLANNER_SPEC,
    GeminiQuestionPlannerSpec,
)
from app.agent.planning.errors import QuestionPlannerResponseInvalidError
from app.agent.planning.plan_draft import QuestionPlanDraft
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import (
    GeminiStateReason,
    output_blocked_reason,
    translate_gemini_error,
)
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.config import settings

_BLOCKED_FINISH_REASONS = frozenset({"SAFETY", "RECITATION"})


class GeminiQuestionPlannerResponseDefect(StrEnum):
    """Gemini planner adapter が検知する response envelope 違反。"""

    NOT_JSON = "question_planner_response_gemini_not_json"
    NOT_OBJECT = "question_planner_response_gemini_not_object"


class GeminiQuestionPlanner:
    """Gemini API backed question plan draft generator."""

    SPEC: Final[GeminiQuestionPlannerSpec] = GEMINI_QUESTION_PLANNER_SPEC

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError(reason=GeminiStateReason.NOT_CONFIGURED)
        self._client = genai.Client(api_key=api_key)

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def prompt_version(self) -> str:
        return self.SPEC.version

    @property
    def rate_limit_policy(self) -> AIModelRateLimitPolicy:
        return self.SPEC.rate_limit_policy

    async def plan(
        self,
        input: AnswerQuestionInput,
        *,
        previous_error: str | None = None,
    ) -> QuestionPlanDraft:
        prompt = GeminiQuestionPlannerPrompt.render(
            question=input.question,
            as_of=input.as_of,
            previous_error=previous_error,
        )
        try:
            return await self._call_api(prompt)
        except (
            AIProviderOutputBlockedError,
            QuestionPlannerResponseInvalidError,
            ValidationError,
        ):
            raise
        except Exception as exc:
            raise translate_gemini_error(exc) from exc

    async def _call_api(self, prompt: str) -> QuestionPlanDraft:
        """Gemini の structured JSON response を draft に詰める。"""

        response = await self._client.aio.models.generate_content(
            model=self.SPEC.model,
            contents=prompt,
            config=GenerateContentConfig(
                **self.SPEC.gen_config,
                **self.SPEC.structured_output,
                response_schema=dict(self.SPEC.response_schema),
            ),
        )

        finish_reason_name = self._extract_finish_reason_name(response)
        if (
            finish_reason_name is not None
            and finish_reason_name in _BLOCKED_FINISH_REASONS
        ):
            raise AIProviderOutputBlockedError(
                reason=output_blocked_reason(finish_reason_name)
            )

        text = response.text or ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise QuestionPlannerResponseInvalidError(
                GeminiQuestionPlannerResponseDefect.NOT_JSON
            ) from exc

        if not isinstance(payload, dict):
            raise QuestionPlannerResponseInvalidError(
                GeminiQuestionPlannerResponseDefect.NOT_OBJECT
            )

        return QuestionPlanDraft.model_validate(payload)

    @staticmethod
    def _extract_finish_reason_name(response: object) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        finish_reason = getattr(candidates[0], "finish_reason", None)
        if finish_reason is None:
            return None
        return getattr(finish_reason, "name", None) or str(finish_reason)
