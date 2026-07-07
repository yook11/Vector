"""Gemini implementation of the evidence answer draft generator."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Final

from google import genai
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.agent.answering.ai.gemini_prompt import GeminiEvidenceAnswerPrompt
from app.agent.answering.ai.gemini_spec import (
    GEMINI_EVIDENCE_ANSWER_SPEC,
    GeminiEvidenceAnswerSpec,
)
from app.agent.answering.evidence import AnswerEvidenceItem
from app.agent.answering.synthesis import (
    AnswerDraftGenerationInvalidError,
    RawAnswerDraft,
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


class GeminiEvidenceAnswerResponseDefect(StrEnum):
    """Gemini evidence answer adapter が検知する response envelope 違反。"""

    NOT_JSON = "evidence_answer_response_gemini_not_json"
    NOT_OBJECT = "evidence_answer_response_gemini_not_object"


class GeminiEvidenceAnswerResponseInvalidError(AnswerDraftGenerationInvalidError):
    """Evidence answer response が ``RawAnswerDraft`` として消化できない。"""

    def __init__(self, defect: GeminiEvidenceAnswerResponseDefect) -> None:
        self.defect = defect
        super().__init__(defect.value)


class GeminiEvidenceAnswerDraftGenerator:
    """Gemini API backed evidence answer draft generator."""

    SPEC: Final[GeminiEvidenceAnswerSpec] = GEMINI_EVIDENCE_ANSWER_SPEC

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
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        previous_error: str | None = None,
    ) -> RawAnswerDraft:
        prompt = GeminiEvidenceAnswerPrompt.render(
            question=question,
            evidence=evidence,
            as_of=as_of,
            target_time_window=target_time_window,
            previous_error=previous_error,
        )
        try:
            return await self._call_api(prompt)
        except (
            AIProviderOutputBlockedError,
            GeminiEvidenceAnswerResponseInvalidError,
            ValidationError,
        ):
            raise

    async def _call_api(self, prompt: str) -> RawAnswerDraft:
        """Gemini の structured JSON response を raw draft に詰める。"""

        try:
            response = await self._client.aio.models.generate_content(
                model=self.SPEC.model,
                contents=prompt,
                config=GenerateContentConfig(
                    **self.SPEC.gen_config,
                    **self.SPEC.structured_output,
                    response_schema=dict(self.SPEC.response_schema),
                ),
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

        text = response.text or ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeminiEvidenceAnswerResponseInvalidError(
                GeminiEvidenceAnswerResponseDefect.NOT_JSON
            ) from exc

        if not isinstance(payload, dict):
            raise GeminiEvidenceAnswerResponseInvalidError(
                GeminiEvidenceAnswerResponseDefect.NOT_OBJECT
            )

        return RawAnswerDraft.model_validate(payload)

    @staticmethod
    def _extract_finish_reason_name(response: object) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        finish_reason = getattr(candidates[0], "finish_reason", None)
        if finish_reason is None:
            return None
        return getattr(finish_reason, "name", None) or str(finish_reason)
