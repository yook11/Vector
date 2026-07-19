"""Gemini-backed one-attempt Agent runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import logfire
from google.genai.client import AsyncClient
from google.genai.types import GenerateContentConfig
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_PROVIDER_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import SpanKind, StatusCode

from app.agent.agent import Agent
from app.agent.runtime._structured_output import (
    parse_json_object,
    thaw_schema,
    validate_output,
)
from app.agent.runtime.contract import AgentResponseInvalidError, AgentTextStream
from app.analysis.ai_provider_errors import (
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

_SPAN_NAME = "agent_provider_call"
_BLOCKED_FINISH_REASONS = frozenset({"SAFETY", "RECITATION"})
_GEN_AI_REASONING_OUTPUT_TOKENS = "gen_ai.usage.reasoning.output_tokens"
_MISSING_OUTPUT = object()
_TRACER = trace.get_tracer(__name__)


class GeminiAgentRuntime:
    """借りたGemini async clientで1 provider attemptだけを実行する。"""

    __slots__ = ("_client",)

    def __init__(self, *, client: AsyncClient) -> None:
        self._client = client

    async def invoke[InputT, OutputT](
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
            raise ValueError("GeminiAgentRuntime.invoke requires response_schema")

        contents = agent.prompt.input_renderer(input)
        config = _build_config(agent, structured=True)
        classified_error: Exception | None = None
        output: OutputT | object = _MISSING_OUTPUT

        span_attributes = {
            "agent_name": agent.name,
            "attempt_number": attempt_number,
            "prompt_version": agent.prompt.version,
            GEN_AI_OPERATION_NAME: "generate_content",
            GEN_AI_PROVIDER_NAME: "gcp.gemini",
            GEN_AI_REQUEST_MODEL: agent.model.name,
        }
        with logfire.span(
            _SPAN_NAME,
            _span_kind=SpanKind.CLIENT,
            **span_attributes,
        ) as span:
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
                _record_classified_error(
                    span,
                    result="provider_error",
                    error_type=_provider_error_type(translated_error),
                )
            else:
                _record_usage(span, getattr(response, "usage_metadata", None))
                finish_reason = _finish_reason_name(response)
                if finish_reason in _BLOCKED_FINISH_REASONS:
                    classified_error = AIProviderOutputBlockedError(
                        reason=output_blocked_reason(finish_reason)
                    )
                    _record_classified_error(
                        span,
                        result="blocked",
                        error_type=_provider_error_type(classified_error),
                    )
                else:
                    try:
                        output = _parse_output(agent, response)
                    except AgentResponseInvalidError as exc:
                        classified_error = exc
                        _record_classified_error(
                            span,
                            result="invalid_response",
                            error_type=exc.defect.value,
                        )
                    else:
                        span.set_attribute("result", "succeeded")

        if classified_error is not None:
            raise classified_error
        if output is _MISSING_OUTPUT:
            raise RuntimeError("Gemini runtime completed without output")
        return cast(OutputT, output)

    def invoke_stream[InputT, OutputT](
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
        span = _TRACER.start_span(
            _SPAN_NAME,
            context=parent_context,
            kind=SpanKind.CLIENT,
            attributes={
                "agent_name": agent.name,
                "attempt_number": attempt_number,
                "prompt_version": agent.prompt.version,
                GEN_AI_OPERATION_NAME: "generate_content",
                GEN_AI_PROVIDER_NAME: "gcp.gemini",
                GEN_AI_REQUEST_MODEL: agent.model.name,
            },
        )
        sdk_stream: AsyncIterator[object] | None = None
        classified_error: Exception | None = None
        translated_cause: Exception | None = None
        unknown_error: Exception | None = None
        terminal_reason_seen = False
        normal_eof = False
        try:
            try:
                sdk_stream = await self._client.models.generate_content_stream(
                    model=agent.model.name,
                    contents=contents,
                    config=config,
                )
                async for chunk in sdk_stream:
                    _record_usage(span, getattr(chunk, "usage_metadata", None))
                    if _has_prompt_block(chunk):
                        classified_error = AIProviderInputRejectedError(
                            reason=GeminiContentRejectionReason.INPUT_BLOCKED
                        )
                        break

                    finish_reason_names = _extract_finish_reason_names(chunk)
                    blocked_reason_name = next(
                        (
                            reason
                            for reason in finish_reason_names
                            if reason in _BLOCKED_FINISH_REASONS
                        ),
                        None,
                    )
                    if blocked_reason_name is not None:
                        classified_error = AIProviderOutputBlockedError(
                            reason=output_blocked_reason(blocked_reason_name)
                        )
                        break
                    terminal_reason_seen = terminal_reason_seen or bool(
                        finish_reason_names
                    )

                    text = getattr(chunk, "text", None)
                    if text:
                        yield text

                if classified_error is None:
                    if terminal_reason_seen:
                        normal_eof = True
                    else:
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

            if normal_eof:
                span.set_attribute("result", "succeeded")
            elif classified_error is not None:
                result = (
                    "blocked"
                    if isinstance(classified_error, AIProviderOutputBlockedError)
                    else "provider_error"
                )
                _record_classified_error(
                    span,
                    result=result,
                    error_type=_provider_error_type(classified_error),
                )
            elif unknown_error is not None:
                span.record_exception(unknown_error)
                span.set_status(StatusCode.ERROR, str(unknown_error))
        finally:
            span.end()

        if classified_error is not None:
            if translated_cause is not None:
                raise classified_error from translated_cause
            raise classified_error
        if unknown_error is not None:
            raise unknown_error


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


def _record_usage(span: Any, usage: object | None) -> None:
    if usage is None:
        return
    fields = (
        ("prompt_token_count", GEN_AI_USAGE_INPUT_TOKENS),
        ("candidates_token_count", GEN_AI_USAGE_OUTPUT_TOKENS),
        (
            "cached_content_token_count",
            GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
        ),
        ("thoughts_token_count", _GEN_AI_REASONING_OUTPUT_TOKENS),
    )
    for source_name, attribute_name in fields:
        value = getattr(usage, source_name, None)
        if value is not None:
            span.set_attribute(attribute_name, value)


def _record_classified_error(
    span: Any,
    *,
    result: str,
    error_type: str,
) -> None:
    span.set_attribute("result", result)
    span.set_attribute(ERROR_TYPE, error_type)
    span.set_status(StatusCode.ERROR)


def _provider_error_type(error: Exception) -> str:
    code = getattr(error, "CODE", None)
    if isinstance(code, str):
        return code
    return type(error).__name__
