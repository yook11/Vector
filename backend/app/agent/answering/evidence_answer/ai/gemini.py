"""Gemini implementation of the evidence answer draft generator."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Final

from google import genai
from google.genai.types import GenerateContentConfig

from app.agent.answering.evidence_answer.ai.prompt import GeminiEvidenceAnswerPrompt
from app.agent.answering.evidence_answer.ai.spec import (
    GEMINI_EVIDENCE_ANSWER_SPEC,
    GeminiEvidenceAnswerSpec,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
    output_blocked_reason,
    translate_gemini_error,
)
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.config import settings

_BLOCKED_FINISH_REASONS = frozenset({"SAFETY", "RECITATION"})


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

    async def stream(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
        previous_error: str | None = None,
    ) -> AsyncIterator[str]:
        prompt = GeminiEvidenceAnswerPrompt.render(
            question=question,
            evidence=evidence,
            as_of=as_of,
            target_time_window=target_time_window,
            user_intent=user_intent,
            prior_coverage=prior_coverage,
            user_activity_context=user_activity_context,
            previous_error=previous_error,
        )
        sdk_stream: AsyncIterator[object] | None = None
        terminal_reason_seen = False
        try:
            sdk_stream = await self._client.aio.models.generate_content_stream(
                model=self.SPEC.model,
                contents=prompt,
                config=GenerateContentConfig(
                    **self.SPEC.gen_config,
                    **self.SPEC.structured_output,
                    response_schema=dict(self.SPEC.response_schema),
                ),
            )
            async for chunk in sdk_stream:
                if self._has_prompt_block(chunk):
                    raise AIProviderInputRejectedError(
                        reason=GeminiContentRejectionReason.INPUT_BLOCKED
                    )

                finish_reason_names = self._extract_finish_reason_names(chunk)
                blocked_reason_name = next(
                    (
                        reason
                        for reason in finish_reason_names
                        if reason in _BLOCKED_FINISH_REASONS
                    ),
                    None,
                )
                if blocked_reason_name is not None:
                    raise AIProviderOutputBlockedError(
                        reason=output_blocked_reason(blocked_reason_name)
                    )
                terminal_reason_seen = terminal_reason_seen or bool(finish_reason_names)

                text = getattr(chunk, "text", None)
                if text:
                    yield text

            if not terminal_reason_seen:
                raise AIProviderNetworkError(reason=GeminiStateReason.STREAM_TRUNCATED)
        except AIProviderError:
            raise
        except Exception as exc:
            translated = translate_gemini_error(exc)
            if translated is exc:
                raise
            raise translated from exc
        finally:
            await _close_stream(sdk_stream)

    @staticmethod
    def _has_prompt_block(response: object) -> bool:
        prompt_feedback = getattr(response, "prompt_feedback", None)
        return (
            prompt_feedback is not None
            and getattr(prompt_feedback, "block_reason", None) is not None
        )

    @staticmethod
    def _extract_finish_reason_names(response: object) -> list[str]:
        names: list[str] = []
        for candidate in getattr(response, "candidates", None) or []:
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason is None:
                continue
            name = getattr(finish_reason, "name", None) or str(finish_reason)
            if name:
                names.append(name)
        return names


async def _close_stream(stream: AsyncIterator[object] | None) -> None:
    if stream is None:
        return
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        return
