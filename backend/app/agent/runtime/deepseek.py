"""DeepSeek-backed one-attempt Agent runtime。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from openai import AsyncOpenAI

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
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.agent.runtime.llm_failure import llm_attempt_failed_from
from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from app.analysis.deepseek_error_translator import translate_deepseek_error

DEEPSEEK_BASE_URL: Final[str] = "https://api.deepseek.com/beta"
DEEPSEEK_CLIENT_TIMEOUT_SECONDS: Final[int] = 20


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
        async with self._llm_calls.record(
            agent_name=agent.name,
            provider=agent.model.provider,
            model=agent.model.name,
            attempt_number=attempt_number,
            prompt_version=agent.prompt.version,
            operation_name="chat",
            gen_ai_provider="deepseek",
            mode="call",
        ) as recording:
            classified_error: Exception | None = None
            try:
                response = await self._client.chat.completions.create(**request)
            except Exception as exc:
                translated_error = translate_deepseek_error(exc)
                if translated_error is exc:
                    raise
                classified_error = translated_error
            else:
                extracted = _usage_from_response(getattr(response, "usage", None))
                if extracted is not None:
                    recording.report_usage(extracted)
                try:
                    return _parse_output(
                        agent,
                        response,
                        binding=self._binding,
                    )
                except AgentResponseInvalidError as exc:
                    classified_error = exc

            if classified_error is not None:
                span_result = (
                    "invalid_response"
                    if isinstance(classified_error, AgentResponseInvalidError)
                    else "provider_error"
                )
                _report_classified(
                    recording,
                    classified_error,
                    span_result=span_result,
                    provider=agent.model.provider,
                )
                raise classified_error


def _report_classified(
    recording: LlmCallRecording,
    error: Exception,
    *,
    span_result: str,
    provider: str,
) -> None:
    recording.report_outcome(llm_attempt_failed_from(error), span_result=span_result)
    record_ai_provider_exhausted(error, provider=provider)


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


def _usage_from_response(usage: object | None) -> Usage | None:
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return _usage_from_optional_counts(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        cache_read_input_tokens=getattr(prompt_details, "cached_tokens", None),
        reasoning_output_tokens=getattr(completion_details, "reasoning_tokens", None),
    )
