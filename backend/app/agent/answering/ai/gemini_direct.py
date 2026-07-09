"""Gemini implementation of the direct answer generator."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from google import genai
from google.genai.types import GenerateContentConfig

from app.agent.answering.ai.gemini_direct_prompt import GeminiDirectAnswerPrompt
from app.agent.answering.ai.gemini_direct_spec import (
    GEMINI_DIRECT_ANSWER_SPEC,
    GeminiDirectAnswerSpec,
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


class GeminiDirectAnswerGenerator:
    """Gemini API backed direct answer generator."""

    SPEC: Final[GeminiDirectAnswerSpec] = GEMINI_DIRECT_ANSWER_SPEC

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
        as_of: datetime,
        user_intent: str = "",
        user_activity_context: str = "",
        previous_answer: str = "",
        previous_error: str | None = None,
    ) -> str:
        prompt = GeminiDirectAnswerPrompt.render(
            question=question,
            as_of=as_of,
            user_intent=user_intent,
            user_activity_context=user_activity_context,
            previous_answer=previous_answer,
            previous_error=previous_error,
        )
        return await self._call_api(prompt)

    async def _call_api(self, prompt: str) -> str:
        """Gemini の plain text response を返す。"""

        try:
            response = await self._client.aio.models.generate_content(
                model=self.SPEC.model,
                contents=prompt,
                config=GenerateContentConfig(**self.SPEC.gen_config),
            )
        except Exception as exc:
            raise translate_gemini_error(exc) from exc

        finish_reason_name = self._extract_finish_reason_name(response)
        if (
            finish_reason_name is not None
            and finish_reason_name in _BLOCKED_FINISH_REASONS
        ):
            raise AIProviderOutputBlockedError(
                reason=output_blocked_reason(finish_reason_name)
            )

        return response.text or ""

    @staticmethod
    def _extract_finish_reason_name(response: object) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        finish_reason = getattr(candidates[0], "finish_reason", None)
        if finish_reason is None:
            return None
        return getattr(finish_reason, "name", None) or str(finish_reason)
