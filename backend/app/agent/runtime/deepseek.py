"""DeepSeek-backed one-attempt Agent runtime。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final, cast

import logfire
from openai import AsyncOpenAI
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
from app.agent.error_type import span_error_type
from app.agent.recording.llm import (
    LlmCallRecorder,
    logfire_llm_call_recorder,
    outcome_from_span_result,
    usage_from_deepseek_usage,
)
from app.agent.recording.types import LlmCallResult, Usage
from app.agent.runtime._structured_output import (
    parse_json_object,
    thaw_schema,
    validate_output,
)
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from app.analysis.deepseek_error_translator import translate_deepseek_error

DEEPSEEK_BASE_URL: Final[str] = "https://api.deepseek.com/beta"
DEEPSEEK_CLIENT_TIMEOUT_SECONDS: Final[int] = 20

_SPAN_NAME = "agent_provider_call"
_GEN_AI_REASONING_OUTPUT_TOKENS = "gen_ai.usage.reasoning.output_tokens"
_MISSING_OUTPUT = object()


@dataclass(frozen=True, slots=True)
class DeepSeekOutputBinding:
    """DeepSeek function callingでdeclared outputを受け取るtransport設定。"""

    function_name: str
    description: str


class DeepSeekAgentRuntime:
    """借りたDeepSeek clientで1 provider attemptだけを実行する。"""

    __slots__ = ("_binding", "_client", "_llm_calls")

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        binding: DeepSeekOutputBinding,
        llm_calls: LlmCallRecorder = logfire_llm_call_recorder,
    ) -> None:
        self._client = client
        self._binding = binding
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
        if agent.model.provider != "deepseek":
            raise ValueError("DeepSeekAgentRuntime requires a DeepSeek Agent")
        if agent.response_schema is None:
            raise ValueError("DeepSeekAgentRuntime requires response_schema")

        request = _build_request(agent, input, binding=self._binding)
        classified_error: Exception | None = None
        output: OutputT | object = _MISSING_OUTPUT
        result: LlmCallResult | None = None
        usage: Usage | None = None
        stopped = False
        llm_call = self._llm_calls.start(
            agent_name=agent.name,
            provider=agent.model.provider,
            model=agent.model.name,
            attempt_number=attempt_number,
        )

        span_attributes = {
            "agent_name": agent.name,
            "attempt_number": attempt_number,
            "prompt_version": agent.prompt.version,
            GEN_AI_OPERATION_NAME: "chat",
            GEN_AI_PROVIDER_NAME: "deepseek",
            GEN_AI_REQUEST_MODEL: agent.model.name,
        }
        try:
            with logfire.span(
                _SPAN_NAME,
                _span_kind=SpanKind.CLIENT,
                **span_attributes,
            ) as span:
                try:
                    response = await self._client.chat.completions.create(**request)
                except Exception as exc:
                    translated_error = translate_deepseek_error(exc)
                    if translated_error is exc:
                        raise
                    classified_error = translated_error
                    _record_classified_error(
                        span,
                        result="provider_error",
                        error_type=span_error_type(translated_error),
                    )
                    record_ai_provider_exhausted(
                        translated_error, provider=agent.model.provider
                    )
                    result = outcome_from_span_result("provider_error")
                else:
                    usage = _record_usage(span, getattr(response, "usage", None))
                    try:
                        output = _parse_output(
                            agent,
                            response,
                            binding=self._binding,
                        )
                    except AgentResponseInvalidError as exc:
                        classified_error = exc
                        _record_classified_error(
                            span,
                            result="invalid_response",
                            error_type=span_error_type(exc),
                        )
                        result = outcome_from_span_result("invalid_response")
                    else:
                        span.set_attribute("result", "succeeded")
                        result = outcome_from_span_result("succeeded")

            if classified_error is not None:
                raise classified_error
            if output is _MISSING_OUTPUT:
                raise RuntimeError("DeepSeek runtime completed without output")
            return cast(OutputT, output)
        except (asyncio.CancelledError, GeneratorExit):
            stopped = True
            result = None
            raise
        finally:
            self._llm_calls.end(
                llm_call,
                result=result,
                usage=usage,
                stopped=stopped,
            )


def _build_request[InputT, OutputT](
    agent: Agent[InputT, OutputT],
    input: InputT,
    *,
    binding: DeepSeekOutputBinding,
) -> dict[str, Any]:
    rendered_input = agent.prompt.input_renderer(input)
    request: dict[str, Any] = {
        "model": agent.model.name,
        "messages": [
            {"role": "system", "content": agent.prompt.instructions},
            {"role": "user", "content": rendered_input},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": binding.function_name,
                    "strict": True,
                    "description": binding.description,
                    "parameters": thaw_schema(agent.response_schema),
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {"name": binding.function_name},
        },
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    if agent.model_settings.temperature is not None:
        request["temperature"] = agent.model_settings.temperature
    if agent.model_settings.max_output_tokens is not None:
        request["max_tokens"] = agent.model_settings.max_output_tokens
    return request


def _parse_output[InputT, OutputT](
    agent: Agent[InputT, OutputT],
    response: object,
    *,
    binding: DeepSeekOutputBinding,
) -> OutputT:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise _missing_declared_output()

    message = getattr(choices[0], "message", None)
    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        raise _missing_declared_output()

    function = getattr(tool_calls[0], "function", None)
    function_name = getattr(function, "name", None)
    if function_name != binding.function_name:
        raise _missing_declared_output()

    raw_arguments = getattr(function, "arguments", None)
    if not isinstance(raw_arguments, str):
        raw_arguments = ""
    return validate_output(agent, parse_json_object(raw_arguments))


def _missing_declared_output() -> AgentResponseInvalidError:
    return AgentResponseInvalidError(
        AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH,
        repair_hint="declared output function call is required",
    )


def _record_usage(span: Any, usage: object | None) -> Usage | None:
    extracted = usage_from_deepseek_usage(usage)
    if extracted is None:
        return None
    if extracted.input_tokens is not None:
        span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, extracted.input_tokens)
    if extracted.output_tokens is not None:
        span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, extracted.output_tokens)
    if extracted.cache_read_input_tokens is not None:
        span.set_attribute(
            GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
            extracted.cache_read_input_tokens,
        )
    if extracted.reasoning_output_tokens is not None:
        span.set_attribute(
            _GEN_AI_REASONING_OUTPUT_TOKENS,
            extracted.reasoning_output_tokens,
        )
    return extracted


def _record_classified_error(
    span: Any,
    *,
    result: str,
    error_type: str,
) -> None:
    span.set_attribute("result", result)
    span.set_attribute(ERROR_TYPE, error_type)
    span.set_status(StatusCode.ERROR)
