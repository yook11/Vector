"""Gemini implementation of question resolution."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Final

from google import genai
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.agent.history.repository import ThreadMessageSnapshot
from app.agent.question_resolution.ai.gemini_prompt import (
    GeminiQuestionResolutionPrompt,
)
from app.agent.question_resolution.ai.gemini_spec import (
    GEMINI_QUESTION_RESOLUTION_SPEC,
    GeminiQuestionResolutionSpec,
)
from app.agent.question_resolution.contract import (
    QuestionResolutionResponseInvalidError,
    ResolvedQuestionDraft,
)
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


class GeminiQuestionResolutionResponseDefect(StrEnum):
    """Malformed Gemini response-envelope categories."""

    NOT_JSON = "question_resolution_response_gemini_not_json"
    NOT_OBJECT = "question_resolution_response_gemini_not_object"


class GeminiQuestionResolver:
    """Gemini-backed structured resolver for one bounded thread window."""

    SPEC: Final[GeminiQuestionResolutionSpec] = GEMINI_QUESTION_RESOLUTION_SPEC

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

    async def resolve(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> ResolvedQuestionDraft:
        prompt = GeminiQuestionResolutionPrompt.render(
            question=question,
            history=history,
            as_of=as_of,
        )
        try:
            return await self._call_api(prompt)
        except (
            AIProviderOutputBlockedError,
            QuestionResolutionResponseInvalidError,
            ValidationError,
        ):
            raise
        except Exception as exc:
            raise translate_gemini_error(exc) from exc

    async def _call_api(self, prompt: str) -> ResolvedQuestionDraft:
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
            raise QuestionResolutionResponseInvalidError(
                GeminiQuestionResolutionResponseDefect.NOT_JSON
            ) from exc
        if not isinstance(payload, dict):
            raise QuestionResolutionResponseInvalidError(
                GeminiQuestionResolutionResponseDefect.NOT_OBJECT
            )
        return ResolvedQuestionDraft.model_validate(payload)

    @staticmethod
    def _extract_finish_reason_name(response: object) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        finish_reason = getattr(candidates[0], "finish_reason", None)
        if finish_reason is None:
            return None
        return getattr(finish_reason, "name", None) or str(finish_reason)
