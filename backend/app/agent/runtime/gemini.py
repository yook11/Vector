"""Gemini-backed one-attempt Agent runtime."""

from __future__ import annotations

from typing import Any, cast

import logfire
from google.genai.client import AsyncClient
from google.genai.types import GenerateContentConfig
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
from app.agent.runtime.contract import AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderOutputBlockedError
from app.analysis.gemini_error_translator import (
    output_blocked_reason,
    translate_gemini_error,
)

_SPAN_NAME = "agent_provider_call"
_BLOCKED_FINISH_REASONS = frozenset({"SAFETY", "RECITATION"})
_GEN_AI_REASONING_OUTPUT_TOKENS = "gen_ai.usage.reasoning.output_tokens"
_MISSING_OUTPUT = object()


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

        contents = agent.prompt.input_renderer(input)
        config = _build_config(agent)
        classified_error: Exception | None = None
        output: OutputT | object = _MISSING_OUTPUT

        span_attributes = {
            "agent_name": agent.name,
            "attempt_number": attempt_number,
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


def _build_config(agent: Agent[Any, Any]) -> GenerateContentConfig:
    config: dict[str, Any] = {
        "system_instruction": agent.prompt.instructions,
        "response_mime_type": "application/json",
        "response_schema": thaw_schema(agent.response_schema),
    }
    if agent.model_settings.temperature is not None:
        config["temperature"] = agent.model_settings.temperature
    if agent.model_settings.max_output_tokens is not None:
        config["max_output_tokens"] = agent.model_settings.max_output_tokens
    return GenerateContentConfig(**config)


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
