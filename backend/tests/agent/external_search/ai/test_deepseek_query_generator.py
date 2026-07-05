"""DeepSeek query generator adapter tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from openai import APIConnectionError, APITimeoutError
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import SecretStr

from app.agent.external_search import ExternalQueryGenerationError
from app.agent.external_search.ai.spec import (
    DEEPSEEK_QUERY_GENERATOR_SPEC,
    EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS,
)
from app.analysis.ai_provider_errors import AIProviderConfigurationError
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from app.config import settings
from tests.agent.external_search.ai.helpers import (
    DEEPSEEK_AS_OF,
    make_request,
    make_response,
    make_status_error,
    patch_adapter_call,
    stub_response,
    task,
)


@pytest.mark.asyncio
async def test_generate_returns_queries_from_tool_arguments() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekQueryGenerator

    generator = DeepSeekQueryGenerator()
    patch_adapter_call(
        generator,
        response=stub_response(
            arguments=json.dumps(
                {"queries": ["NVIDIA Blackwell supply", "NVIDIA earnings AI"]}
            )
        ),
    )

    queries = await generator.generate(
        task=task(),
        as_of=DEEPSEEK_AS_OF,
        target_time_window="直近24時間",
    )

    assert queries == ["NVIDIA Blackwell supply", "NVIDIA earnings AI"]


@pytest.mark.asyncio
async def test_generate_sends_structured_output_mechanism_to_sdk() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekQueryGenerator

    generator = DeepSeekQueryGenerator()
    mock_call = patch_adapter_call(
        generator,
        response=stub_response(arguments=json.dumps({"queries": ["NVIDIA news"]})),
    )

    await generator.generate(
        task=task("SDK_PROMPT_MARKER"),
        as_of=DEEPSEEK_AS_OF,
        target_time_window="直近24時間",
    )

    kwargs = mock_call.await_args.kwargs
    assert kwargs["model"] == DEEPSEEK_QUERY_GENERATOR_SPEC.model
    assert kwargs["messages"][0]["role"] == "user"
    assert "SDK_PROMPT_MARKER" in kwargs["messages"][0]["content"]
    assert kwargs["tools"][0]["function"]["name"] == (
        DEEPSEEK_QUERY_GENERATOR_SPEC.tool_name
    )
    assert kwargs["tools"][0]["function"]["strict"] is True
    assert kwargs["tools"][0]["function"]["parameters"] == dict(
        DEEPSEEK_QUERY_GENERATOR_SPEC.response_schema
    )
    assert (
        kwargs["tool_choice"]["function"]["name"]
        == DEEPSEEK_QUERY_GENERATOR_SPEC.tool_name
    )
    assert kwargs["extra_body"]["thinking"]["type"] == "disabled"
    assert (
        kwargs["max_tokens"] == DEEPSEEK_QUERY_GENERATOR_SPEC.gen_config["max_tokens"]
    )


@pytest.mark.asyncio
async def test_generate_returns_only_string_queries() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekQueryGenerator

    generator = DeepSeekQueryGenerator()
    patch_adapter_call(
        generator,
        response=stub_response(
            arguments=json.dumps({"queries": ["NVIDIA news", 123, None, "TSMC"]})
        ),
    )

    queries = await generator.generate(
        task=task(),
        as_of=DEEPSEEK_AS_OF,
        target_time_window=None,
    )

    assert queries == ["NVIDIA news", "TSMC"]


@pytest.mark.parametrize(
    "response,expected_reason",
    [
        (
            stub_response(arguments=None, no_tool_calls=True),
            "external_search_deepseek_no_tool_call",
        ),
        (
            stub_response(arguments="{}", tool_name="wrong_tool"),
            "external_search_deepseek_wrong_tool_name",
        ),
        (
            stub_response(arguments="RAW_RESPONSE_MARKER not json"),
            "external_search_deepseek_arguments_not_json",
        ),
        (
            stub_response(arguments="[1, 2, 3]"),
            "external_search_deepseek_arguments_not_dict",
        ),
        (
            stub_response(arguments=json.dumps({"not_queries": []})),
            "external_search_deepseek_arguments_schema_invalid",
        ),
        (
            stub_response(arguments=json.dumps({"queries": "NVIDIA"})),
            "external_search_deepseek_arguments_schema_invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_generate_wraps_envelope_defects_with_reason(
    response: MagicMock,
    expected_reason: str,
) -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekQueryGenerator

    generator = DeepSeekQueryGenerator()
    patch_adapter_call(generator, response=response)

    with pytest.raises(ExternalQueryGenerationError) as exc_info:
        await generator.generate(
            task=task("PROMPT_MARKER"),
            as_of=DEEPSEEK_AS_OF,
            target_time_window="TIME_WINDOW_MARKER",
        )

    assert exc_info.value.reason == expected_reason
    assert str(exc_info.value) == expected_reason
    assert "PROMPT_MARKER" not in str(exc_info.value)
    assert "TIME_WINDOW_MARKER" not in str(exc_info.value)
    assert "RAW_RESPONSE_MARKER" not in str(exc_info.value)
    assert "SECRET_API_KEY" not in str(exc_info.value)


@pytest.mark.parametrize(
    "side_effect,expected_reason",
    [
        (
            APITimeoutError(request=make_request()),
            DeepSeekStateReason.TIMEOUT,
        ),
        (
            APIConnectionError(request=make_request()),
            DeepSeekStateReason.CONNECTION,
        ),
        (
            OpenAIRateLimitError(
                "RAW_RESPONSE_MARKER rate",
                response=make_response(429),
                body=None,
            ),
            DeepSeekStateReason.RATE_LIMITED,
        ),
        (
            make_status_error(402, "RAW_RESPONSE_MARKER insufficient"),
            DeepSeekStateReason.INSUFFICIENT_BALANCE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_generate_wraps_sdk_errors_without_leaking_sdk_type_or_message(
    side_effect: Exception,
    expected_reason: DeepSeekStateReason,
) -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekQueryGenerator

    generator = DeepSeekQueryGenerator()
    patch_adapter_call(generator, side_effect=side_effect)

    with pytest.raises(ExternalQueryGenerationError) as exc_info:
        await generator.generate(
            task=task("PROMPT_MARKER"),
            as_of=DEEPSEEK_AS_OF,
            target_time_window="TIME_WINDOW_MARKER",
        )

    assert exc_info.value.reason == expected_reason
    assert "RAW_RESPONSE_MARKER" not in str(exc_info.value)
    assert "PROMPT_MARKER" not in str(exc_info.value)
    assert "TIME_WINDOW_MARKER" not in str(exc_info.value)
    assert "SECRET_API_KEY" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_generate_propagates_untranslated_exceptions() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekQueryGenerator

    generator = DeepSeekQueryGenerator()
    original = ValueError("untranslated failure")
    patch_adapter_call(generator, side_effect=original)

    with pytest.raises(ValueError, match="untranslated failure"):
        await generator.generate(
            task=task(),
            as_of=DEEPSEEK_AS_OF,
            target_time_window=None,
        )


def test_empty_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekQueryGenerator

    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr(""))

    with pytest.raises(AIProviderConfigurationError) as exc_info:
        DeepSeekQueryGenerator()

    assert exc_info.value.reason is DeepSeekStateReason.NOT_CONFIGURED


def test_client_construction_receives_timeout_and_does_not_leak_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.external_search.ai import deepseek as deepseek_module

    calls: list[dict[str, object]] = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr("SECRET_API_KEY"))
    monkeypatch.setattr(deepseek_module, "AsyncOpenAI", FakeAsyncOpenAI)

    deepseek_module.DeepSeekQueryGenerator()

    assert calls == [
        {
            "api_key": "SECRET_API_KEY",
            "base_url": DEEPSEEK_QUERY_GENERATOR_SPEC.base_url,
            "timeout": EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS,
        }
    ]
