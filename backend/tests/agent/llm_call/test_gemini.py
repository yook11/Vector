"""GeminiLlmCaller の one-attempt behavior tests."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
from google.genai import errors as genai_errors

from app.agent.llm_call.gemini import GeminiLlmCaller
from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderTransportError,
)
from tests.agent.llm_call._helpers import (
    FakeGeminiClient,
    FakeResponse,
    make_agent,
    success_response,
)
from tests.cloudwatch.records import metric_records


async def test_call_uses_provider_once_and_returns_the_json_object() -> None:
    """一試行が一度だけ provider を呼び、返った JSON object をそのまま返す。"""
    client = FakeGeminiClient([success_response(result="validated")])
    caller = GeminiLlmCaller(client=client)

    output = await caller.call(make_agent(), "typed input", attempt_number=1)

    assert output == {"result": "validated", "tags": ["caller"]}
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_call_does_not_validate_the_declared_output_type() -> None:
    """宣言された出力の型に合わない JSON object も、検証せずにそのまま返す。"""
    payload = {"result": 1, "unknown": "kept"}
    client = FakeGeminiClient([FakeResponse(text=json.dumps(payload))])
    caller = GeminiLlmCaller(client=client)

    output = await caller.call(make_agent(), "typed input", attempt_number=1)

    assert output == payload


async def test_request_separates_instructions_contents_and_thaws_schema() -> None:
    """指示・入力・schema を provider request で独立して保持する。"""
    client = FakeGeminiClient([success_response()])
    caller = GeminiLlmCaller(client=client)
    instructions = "SYSTEM_INSTRUCTIONS_SENTINEL_77ca"
    contents = "TASK_CONTENTS_SENTINEL_158b"
    agent = make_agent(
        instructions=instructions,
        rendered_input=contents,
        temperature=None,
        max_output_tokens=456,
    )

    await caller.call(agent, object(), attempt_number=1)

    kwargs = client.models.generate_content.await_args.kwargs
    config = kwargs["config"]
    explicit_config = config.model_dump(exclude_unset=True)
    response_schema = config.response_schema
    assert kwargs["model"] == agent.model.name
    assert kwargs["contents"] == contents
    assert config.system_instruction == instructions
    assert config.response_mime_type == "application/json"
    assert explicit_config["max_output_tokens"] == 456
    assert "temperature" not in explicit_config
    assert isinstance(response_schema, dict)
    assert isinstance(response_schema["required"], list)
    assert isinstance(response_schema["properties"], dict)
    assert isinstance(response_schema["properties"]["tags"], dict)
    assert isinstance(response_schema["properties"]["tags"]["items"], dict)


async def test_same_caller_does_not_carry_agent_or_input_state_between_calls() -> None:
    """同一 caller の連続呼出しで agent と入力の状態を持ち越さない。"""
    client = FakeGeminiClient(
        [
            success_response(result="first", tags=["one"]),
            success_response(result="second", tags=["two"]),
        ]
    )
    caller = GeminiLlmCaller(client=client)
    first_agent = make_agent(
        name="first_agent",
        instructions="FIRST_INSTRUCTIONS_SENTINEL",
        rendered_input="FIRST_CONTENTS_SENTINEL",
        model_name="gemini-first-model",
        temperature=0.1,
        max_output_tokens=111,
    )
    second_agent = make_agent(
        name="second_agent",
        instructions="SECOND_INSTRUCTIONS_SENTINEL",
        rendered_input="SECOND_CONTENTS_SENTINEL",
        model_name="gemini-second-model",
        temperature=0.9,
        max_output_tokens=222,
    )

    first_output = await caller.call(first_agent, "first input", attempt_number=1)
    second_output = await caller.call(second_agent, "second input", attempt_number=2)

    first_call, second_call = client.models.generate_content.await_args_list
    assert first_output["result"] == "first"
    assert second_output["result"] == "second"
    assert first_call.kwargs["model"] == "gemini-first-model"
    assert first_call.kwargs["contents"] == "FIRST_CONTENTS_SENTINEL"
    assert first_call.kwargs["config"].system_instruction == (
        "FIRST_INSTRUCTIONS_SENTINEL"
    )
    assert first_call.kwargs["config"].temperature == 0.1
    assert second_call.kwargs["model"] == "gemini-second-model"
    assert second_call.kwargs["contents"] == "SECOND_CONTENTS_SENTINEL"
    assert second_call.kwargs["config"].system_instruction == (
        "SECOND_INSTRUCTIONS_SENTINEL"
    )
    assert second_call.kwargs["config"].temperature == 0.9


async def test_known_gemini_failure_uses_existing_error_translation() -> None:
    """既知の Gemini 障害を既存のアプリケーション例外へ翻訳する。"""
    error = httpx2.ReadTimeout("PROVIDER_SENTINEL_TIMEOUT_79ab")
    client = FakeGeminiClient([error])
    caller = GeminiLlmCaller(client=client)

    with pytest.raises(AIProviderTransportError) as exc_info:
        await caller.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value.__cause__ is error
    assert "PROVIDER_SENTINEL_TIMEOUT_79ab" not in str(exc_info.value)
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_unclassified_exception_propagates_with_identity() -> None:
    """未分類例外の同一性を失わずに伝播させる。"""
    error = RuntimeError("UNCLASSIFIED_EXCEPTION_SENTINEL_2c5e")
    client = FakeGeminiClient([error])
    caller = GeminiLlmCaller(client=client)

    with pytest.raises(RuntimeError) as exc_info:
        await caller.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value is error
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


@pytest.mark.parametrize("attempt_number", [0, -1])
async def test_non_positive_attempt_number_is_rejected_before_provider_call(
    attempt_number: int,
) -> None:
    """非正の試行番号では provider 呼出し前に拒否する。"""
    client = FakeGeminiClient([success_response()])
    caller = GeminiLlmCaller(client=client)

    with pytest.raises(ValueError):
        await caller.call(
            make_agent(),
            "typed input",
            attempt_number=attempt_number,
        )

    client.models.generate_content.assert_not_awaited()


async def test_renderer_failure_propagates_without_provider_call() -> None:
    """入力描画の失敗時に provider を呼び出さない。"""
    error = RuntimeError("RENDERER_FAILURE_SENTINEL_93b0")
    client = FakeGeminiClient([success_response()])
    agent = make_agent()
    agent = replace(
        agent,
        prompt=type(agent.prompt)(
            version=agent.prompt.version,
            instructions=agent.prompt.instructions,
            input_renderer=lambda _input: (_ for _ in ()).throw(error),
        ),
    )
    caller = GeminiLlmCaller(client=client)

    with pytest.raises(RuntimeError) as exc_info:
        await caller.call(agent, "typed input", attempt_number=1)

    assert exc_info.value is error
    client.models.generate_content.assert_not_awaited()


async def test_config_construction_failure_happens_before_provider_call() -> None:
    """request config の不備を provider 呼出し前に表面化させる。"""
    client = FakeGeminiClient([success_response()])
    agent = make_agent()
    agent = replace(
        agent,
        model_settings=type(agent.model_settings)(
            temperature=SimpleNamespace(invalid="temperature"),
            max_output_tokens=321,
        ),
    )
    caller = GeminiLlmCaller(client=client)

    with pytest.raises((TypeError, ValueError)):
        await caller.call(agent, "typed input", attempt_number=1)

    client.models.generate_content.assert_not_awaited()


# ai_provider_exhausted EMF emit (CloudWatch A6) — 枯渇系翻訳のみが打点する


_EXHAUSTED_METRIC = "ai_provider_exhausted"


def _quota_exhausted_sdk_error() -> genai_errors.ClientError:
    """429 + per-day QuotaFailure details の実レスポンス形 (枯渇に翻訳される)。"""
    response_json = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "message": "You exceeded your current quota, check your plan and billing.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                    ],
                }
            ],
        }
    }
    return genai_errors.ClientError(429, response_json)


def _rate_limited_sdk_error() -> genai_errors.ClientError:
    """429 だが details 無し (per-minute バースト等、rate limit に翻訳される)。"""
    response_json = {
        "error": {"status": "RESOURCE_EXHAUSTED", "message": "rate limit reached"}
    }
    return genai_errors.ClientError(429, response_json)


async def test_call_quota_exhausted_sdk_error_emits_ai_provider_exhausted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """quota 超過 (usage_limit_exhausted 翻訳) は provider=gemini で emit する。"""
    client = FakeGeminiClient([_quota_exhausted_sdk_error()])
    caller = GeminiLlmCaller(client=client)

    with pytest.raises(AIProviderResponseError):
        await caller.call(make_agent(), "typed input", attempt_number=1)

    records = metric_records(capsys.readouterr().out, _EXHAUSTED_METRIC)
    assert len(records) == 1
    assert records[0]["kind"] == "quota_exhausted"
    assert records[0]["provider"] == "gemini"


async def test_call_plain_rate_limited_sdk_error_does_not_emit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """一時的 rate limit (時間経過で回復) は枯渇ではないため emit しない。"""
    client = FakeGeminiClient([_rate_limited_sdk_error()])
    caller = GeminiLlmCaller(client=client)

    with pytest.raises(AIProviderResponseError):
        await caller.call(make_agent(), "typed input", attempt_number=1)

    assert metric_records(capsys.readouterr().out, _EXHAUSTED_METRIC) == []
