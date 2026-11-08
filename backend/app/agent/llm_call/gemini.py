"""Geminiで1 provider attemptを実行するLLM呼び出しの本物。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

from google.genai.client import AsyncClient
from google.genai.types import GenerateContentConfig
from opentelemetry import context as otel_context

from app.agent.agent import Agent
from app.agent.llm_call.caller import AgentTextStream
from app.agent.recording.llm import (
    UNCLASSIFIED_FAILURE_CODE,
    LlmAttemptFailed,
    LlmCallRecorder,
    LlmCallRecording,
    llm_attempt_failed_from,
    logfire_llm_call_recorder,
)
from app.agent.recording.types import Usage, _usage_from_optional_counts
from app.ai_providers.errors import AIProviderError, AIProviderResultError
from app.ai_providers.gemini.error_translator import translate_gemini_error
from app.ai_providers.gemini.response import (
    GeminiStreamReader,
    read_gemini_json_object,
)
from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted


class GeminiLlmCaller:
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

    async def call[InputT](
        self,
        agent: Agent[InputT, Any],
        input: InputT,
        *,
        attempt_number: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number <= 0
        ):
            raise ValueError("attempt_number must be a positive integer")
        if agent.model.provider != "gemini":
            raise ValueError("GeminiLlmCaller requires a Gemini Agent")
        if agent.response_schema is None:
            raise ValueError("GeminiLlmCaller.call requires response_schema")

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
            translated_cause: Exception | None = None
            try:
                response = await self._client.models.generate_content(
                    model=agent.model.name,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                translated_error = translate_gemini_error(exc)
                if translated_error is exc:
                    _report_unclassified(recording)
                    raise
                classified_error = translated_error
                translated_cause = exc
            else:
                _report_usage(
                    recording,
                    _usage_from_metadata(getattr(response, "usage_metadata", None)),
                )
                try:
                    return read_gemini_json_object(response)
                except AIProviderResultError as exc:
                    classified_error = exc

            if classified_error is not None:
                _report_classified(
                    recording,
                    classified_error,
                    provider=agent.model.provider,
                )
                if translated_cause is not None:
                    raise classified_error from translated_cause
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
            raise ValueError("GeminiLlmCaller requires a Gemini Agent")

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
            reader = GeminiStreamReader()
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
                    text = reader.read(chunk)
                    if text:
                        yield text
                reader.finish()
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
                _report_classified(
                    recording,
                    classified_error,
                    provider=agent.model.provider,
                )
                if translated_cause is not None:
                    raise classified_error from translated_cause
                raise classified_error
            if unknown_error is not None:
                _report_unclassified(recording)
                raise unknown_error


def _report_classified(
    recording: LlmCallRecording,
    error: Exception,
    *,
    provider: str,
) -> None:
    recording.report_outcome(llm_attempt_failed_from(error))
    record_ai_provider_exhausted(error, provider=provider)


def _report_unclassified(recording: LlmCallRecording) -> None:
    recording.report_outcome(LlmAttemptFailed(failure_code=UNCLASSIFIED_FAILURE_CODE))


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
            response_schema=_thaw_schema(agent.response_schema),
        )
    if agent.model_settings.temperature is not None:
        config["temperature"] = agent.model_settings.temperature
    if agent.model_settings.max_output_tokens is not None:
        config["max_output_tokens"] = agent.model_settings.max_output_tokens
    return GenerateContentConfig(**config)


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


def _usage_from_metadata(usage: object | None) -> Usage | None:
    return _usage_from_optional_counts(
        input_tokens=getattr(usage, "prompt_token_count", None),
        output_tokens=getattr(usage, "candidates_token_count", None),
        cache_read_input_tokens=getattr(usage, "cached_content_token_count", None),
        reasoning_output_tokens=getattr(usage, "thoughts_token_count", None),
    )


def _thaw_schema(value: Any) -> Any:
    """Agentの凍結schemaをprovider request用のJSON互換値へ戻す。"""
    if isinstance(value, Mapping):
        return {key: _thaw_schema(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_schema(item) for item in value]
    return value
