"""Gemini implementation of question context generation."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Final

from google import genai
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.agent.question_context.ai.gemini_prompt import (
    GeminiQuestionContextPrompt,
)
from app.agent.question_context.ai.gemini_spec import (
    GEMINI_QUESTION_CONTEXT_SPEC,
    GeminiQuestionContextSpec,
)
from app.agent.question_context.contract import (
    QuestionContextDraft,
    QuestionContextResponseInvalidError,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
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


class GeminiQuestionContextResponseDefect(StrEnum):
    """Malformed Gemini response-envelope categories."""

    NOT_JSON = "question_resolution_response_gemini_not_json"
    NOT_OBJECT = "question_resolution_response_gemini_not_object"


class GeminiQuestionContextGenerator:
    """Gemini-backed context generator for one bounded thread window."""

    SPEC: Final[GeminiQuestionContextSpec] = GEMINI_QUESTION_CONTEXT_SPEC

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

    async def generate(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> QuestionContextDraft:
        prompt = GeminiQuestionContextPrompt.render(
            question=question,
            history=history,
            as_of=as_of,
        )
        try:
            return await self._call_api(prompt)
        except (
            AIProviderOutputBlockedError,
            QuestionContextResponseInvalidError,
            ValidationError,
        ):
            raise
        except Exception as exc:
            raise translate_gemini_error(exc) from exc

    async def _call_api(self, prompt: str) -> QuestionContextDraft:
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
        if finish_reason_name in _BLOCKED_FINISH_REASONS:
            raise AIProviderOutputBlockedError(
                reason=output_blocked_reason(finish_reason_name)
            )
        try:
            payload = json.loads(response.text or "")
        except json.JSONDecodeError as exc:
            raise QuestionContextResponseInvalidError(
                GeminiQuestionContextResponseDefect.NOT_JSON
            ) from exc
        if not isinstance(payload, dict):
            raise QuestionContextResponseInvalidError(
                GeminiQuestionContextResponseDefect.NOT_OBJECT
            )
        return QuestionContextDraft.model_validate(payload)

    @staticmethod
    def _extract_finish_reason_name(response: object) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        finish_reason = getattr(candidates[0], "finish_reason", None)
        if finish_reason is None:
            return None
        return getattr(finish_reason, "name", None) or str(finish_reason)
