"""Gemini-backed one-attempt Agent runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

from google.genai.client import AsyncClient
from google.genai.types import GenerateContentConfig
from opentelemetry import context as otel_context

from app.agent.agent import Agent
from app.agent.recording.llm import (
    LlmCallRecorder,
    LlmCallRecording,
    logfire_llm_call_recorder,
)
from app.agent.recording.types import Usage, _usage_from_optional_counts
from app.agent.runtime._structured_output import (
    parse_json_object,
    thaw_schema,
    validate_output,
)
from app.agent.runtime.contract import AgentResponseInvalidError, AgentTextStream
from app.agent.runtime.llm_failure import llm_attempt_failed_from
from app.analysis.ai_provider_errors import (
    AIProviderContentRejectionKind,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderOutputTruncatedError,
)
from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from app.analysis.gemini_error_translator import (
    OUTPUT_BLOCKED_FINISH_REASONS,
    GeminiContentRejectionReason,
    GeminiStateReason,
    output_blocked_reason,
    translate_gemini_error,
)


class GeminiAgentRuntime:
    """借りたGemini async clientで1 provider attemptだけを実行する。"""

    __slots__ = ("_client", "_llm_calls")

    def __init__(
        self,
        *,
        client: AsyncClient,
        llm_calls: LlmCallRecorder = logfire_llm_call_recorder,
    ) -> None:
        self._client = client
        self._llm_calls = llm_calls

    async def call[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> OutputT:
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number <= 0
        ):
            raise ValueError("attempt_number must be a positive integer")
        if agent.model.provider != "gemini":
            raise ValueError("GeminiAgentRuntime requires a Gemini Agent")
        if agent.response_schema is None:
            raise ValueError("GeminiAgentRuntime.call requires response_schema")

        contents = agent.prompt.input_renderer(input)
        config = _build_config(agent, structured=True)
        async with self._llm_calls.record(
            agent_name=agent.name,
            provider=agent.model.provider,
            model=agent.model.name,
            attempt_number=attempt_number,
            prompt_version=agent.prompt.version,
            operation_name="generate_content",
            gen_ai_provider="gcp.gemini",
            mode="call",
        ) as recording:
            classified_error: Exception | None = None
            try:
                response = await self._client.models.generate_content(
                    model=agent.model.name,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                translated_error = translate_gemini_error(exc)
                if translated_error is exc:
                    raise
                classified_error = translated_error
            else:
                _report_usage(
                    recording,
                    _usage_from_metadata(getattr(response, "usage_metadata", None)),
                )
                if _has_prompt_block(response):
                    classified_error = AIProviderInputRejectedError(
                        reason=GeminiContentRejectionReason.INPUT_BLOCKED,
                        rejection_kind=AIProviderContentRejectionKind.SAFETY,
                    )
                else:
                    finish_reason = _finish_reason_name(response)
                    if finish_reason in OUTPUT_BLOCKED_FINISH_REASONS:
                        classified_error = AIProviderOutputBlockedError(
                            reason=output_blocked_reason(finish_reason),
                            rejection_kind=(
                                AIProviderContentRejectionKind.SAFETY
                                if finish_reason == "SAFETY"
                                else AIProviderContentRejectionKind.OTHER
                            ),
                        )
                    elif finish_reason == "MAX_TOKENS":
                        classified_error = AIProviderOutputTruncatedError(
                            reason=GeminiStateReason.OUTPUT_TOKEN_LIMIT_REACHED
                        )
                    else:
                        try:
                            return _parse_output(agent, response)
                        except AgentResponseInvalidError as exc:
                            classified_error = exc

            if classified_error is not None:
                if isinstance(
                    classified_error,
                    AIProviderInputRejectedError | AIProviderOutputBlockedError,
                ):
                    span_result = "blocked"
                elif isinstance(classified_error, AgentResponseInvalidError):
                    span_result = "invalid_response"
                else:
                    span_result = "provider_error"
                _report_classified(
                    recording,
                    classified_error,
                    span_result=span_result,
                    provider=agent.model.provider,
                )
                raise classified_error

    def stream_text[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> AgentTextStream:
        """provider streamを初回反復まで遅延し、fragmentを無加工で返す。"""
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number <= 0
        ):
            raise ValueError("attempt_number must be a positive integer")
        if agent.model.provider != "gemini":
            raise ValueError("GeminiAgentRuntime requires a Gemini Agent")

        contents = agent.prompt.input_renderer(input)
        config = _build_config(
            agent,
            structured=agent.response_schema is not None,
        )
        parent_context = otel_context.get_current()
        return cast(
            AgentTextStream,
            self._stream_fragments(
                agent=agent,
                contents=contents,
                config=config,
                attempt_number=attempt_number,
                parent_context=parent_context,
            ),
        )

    async def _stream_fragments[InputT, OutputT](
        self,
        *,
        agent: Agent[InputT, OutputT],
        contents: str,
        config: GenerateContentConfig,
        attempt_number: int,
        parent_context: otel_context.Context,
    ) -> AsyncIterator[str]:
        async with self._llm_calls.record(
            agent_name=agent.name,
            provider=agent.model.provider,
            model=agent.model.name,
            attempt_number=attempt_number,
            prompt_version=agent.prompt.version,
            operation_name="generate_content",
            gen_ai_provider="gcp.gemini",
            mode="stream",
            parent_context=parent_context,
        ) as recording:
            sdk_stream: AsyncIterator[object] | None = None
            classified_error: Exception | None = None
            translated_cause: Exception | None = None
            unknown_error: Exception | None = None
            terminal_reason_seen = False
            try:
                sdk_stream = await self._client.models.generate_content_stream(
                    model=agent.model.name,
                    contents=contents,
                    config=config,
                )
                async for chunk in sdk_stream:
                    _report_usage(
                        recording,
                        _usage_from_metadata(getattr(chunk, "usage_metadata", None)),
                    )
                    if _has_prompt_block(chunk):
                        classified_error = AIProviderInputRejectedError(
                            reason=GeminiContentRejectionReason.INPUT_BLOCKED,
                            rejection_kind=AIProviderContentRejectionKind.SAFETY,
                        )
                        break

                    finish_reason_names = _extract_finish_reason_names(chunk)
                    blocked_reason_name = next(
                        (
                            reason
                            for reason in finish_reason_names
                            if reason in OUTPUT_BLOCKED_FINISH_REASONS
                        ),
                        None,
                    )
                    if blocked_reason_name is not None:
                        classified_error = AIProviderOutputBlockedError(
                            reason=output_blocked_reason(blocked_reason_name),
                            rejection_kind=(
                                AIProviderContentRejectionKind.SAFETY
                                if blocked_reason_name == "SAFETY"
                                else AIProviderContentRejectionKind.OTHER
                            ),
                        )
                        break
                    if "MAX_TOKENS" in finish_reason_names:
                        classified_error = AIProviderOutputTruncatedError(
                            reason=GeminiStateReason.OUTPUT_TOKEN_LIMIT_REACHED
                        )
                        break
                    terminal_reason_seen = terminal_reason_seen or bool(
                        finish_reason_names
                    )

                    text = getattr(chunk, "text", None)
                    if text:
                        yield text

                if classified_error is None and not terminal_reason_seen:
                    classified_error = AIProviderNetworkError(
                        reason=GeminiStateReason.STREAM_TRUNCATED
                    )
            except (GeneratorExit, asyncio.CancelledError):
                raise
            except AIProviderError as exc:
                classified_error = exc
            except Exception as exc:
                translated_error = translate_gemini_error(exc)
                if translated_error is exc:
                    unknown_error = exc
                else:
                    classified_error = translated_error
                    translated_cause = exc
            finally:
                await _close_sdk_stream(sdk_stream)

            if classified_error is not None:
                span_result = (
                    "blocked"
                    if isinstance(classified_error, AIProviderOutputBlockedError)
                    else "provider_error"
                )
                _report_classified(
                    recording,
                    classified_error,
                    span_result=span_result,
                    provider=agent.model.provider,
                )
                if translated_cause is not None:
                    raise classified_error from translated_cause
                raise classified_error
            if unknown_error is not None:
                raise unknown_error


def _report_classified(
    recording: LlmCallRecording,
    error: Exception,
    *,
    span_result: str,
    provider: str,
) -> None:
    recording.report_outcome(llm_attempt_failed_from(error), span_result=span_result)
    record_ai_provider_exhausted(error, provider=provider)


def _report_usage(recording: LlmCallRecording, usage: Usage | None) -> None:
    if usage is not None:
        recording.report_usage(usage)


def _build_config(
    agent: Agent[Any, Any],
    *,
    structured: bool,
) -> GenerateContentConfig:
    config: dict[str, Any] = {
        "system_instruction": agent.prompt.instructions,
    }
    if structured:
        if agent.response_schema is None:
            raise ValueError("structured Gemini request requires response_schema")
        config.update(
            response_mime_type="application/json",
            response_schema=thaw_schema(agent.response_schema),
        )
    if agent.model_settings.temperature is not None:
        config["temperature"] = agent.model_settings.temperature
    if agent.model_settings.max_output_tokens is not None:
        config["max_output_tokens"] = agent.model_settings.max_output_tokens
    return GenerateContentConfig(**config)


def _has_prompt_block(response: object) -> bool:
    prompt_feedback = getattr(response, "prompt_feedback", None)
    return (
        prompt_feedback is not None
        and getattr(prompt_feedback, "block_reason", None) is not None
    )


def _extract_finish_reason_names(response: object) -> list[str]:
    names: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason is None:
            continue
        if isinstance(finish_reason, str):
            names.append(finish_reason)
            continue
        name = getattr(finish_reason, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
            continue
        value = getattr(finish_reason, "value", None)
        if isinstance(value, str) and value:
            names.append(value)
    return names


async def _close_sdk_stream(stream: AsyncIterator[object] | None) -> None:
    if stream is None:
        return
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except asyncio.CancelledError:
        raise
    except Exception:
        return


def _finish_reason_name(response: object) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    finish_reason = getattr(candidates[0], "finish_reason", None)
    if isinstance(finish_reason, str):
        return finish_reason
    for attribute in ("name", "value"):
        value = getattr(finish_reason, attribute, None)
        if isinstance(value, str):
            return value
    return None


def _parse_output[InputT, OutputT](
    agent: Agent[InputT, OutputT],
    response: object,
) -> OutputT:
    text = getattr(response, "text", None) or ""
    return validate_output(agent, parse_json_object(text))


def _usage_from_metadata(usage: object | None) -> Usage | None:
    return _usage_from_optional_counts(
        input_tokens=getattr(usage, "prompt_token_count", None),
        output_tokens=getattr(usage, "candidates_token_count", None),
        cache_read_input_tokens=getattr(usage, "cached_content_token_count", None),
        reasoning_output_tokens=getattr(usage, "thoughts_token_count", None),
    )
