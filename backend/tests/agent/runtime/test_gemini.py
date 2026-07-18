"""GeminiAgentRuntime の one-attempt behavior tests."""

from __future__ import annotations

import json
from dataclasses import replace
from inspect import signature
from types import SimpleNamespace

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from tests.agent.runtime._helpers import (
    FakeGeminiClient,
    FakeResponse,
    RuntimeOutput,
    ValidationProbeOutput,
    blocked_response,
    make_agent,
    required_attribute,
    runtime_contract,
    runtime_type,
    success_response,
)


async def test_constructor_accepts_only_borrowed_async_client() -> None:
    client = FakeGeminiClient([success_response()])
    gemini_runtime_type = runtime_type()

    assert list(signature(gemini_runtime_type).parameters) == ["client"]
    gemini_runtime_type(client=client)


async def test_invoke_calls_provider_once_and_returns_validated_output_directly() -> (
    None
):
    client = FakeGeminiClient([success_response(result="validated")])
    runtime = runtime_type()(client=client)

    output = await runtime.invoke(make_agent(), "typed input", attempt_number=1)

    assert output == RuntimeOutput(result="validated", tags=["runtime"])
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_request_separates_instructions_contents_and_thaws_schema() -> None:
    client = FakeGeminiClient([success_response()])
    runtime = runtime_type()(client=client)
    instructions = "SYSTEM_INSTRUCTIONS_SENTINEL_77ca"
    contents = "TASK_CONTENTS_SENTINEL_158b"
    agent = make_agent(
        instructions=instructions,
        rendered_input=contents,
        temperature=None,
        max_output_tokens=456,
    )

    await runtime.invoke(agent, object(), attempt_number=1)

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


async def test_same_runtime_does_not_carry_agent_or_input_state_between_invokes() -> (
    None
):
    client = FakeGeminiClient(
        [
            success_response(result="first", tags=["one"]),
            success_response(result="second", tags=["two"]),
        ]
    )
    runtime = runtime_type()(client=client)
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

    first_output = await runtime.invoke(first_agent, "first input", attempt_number=1)
    second_output = await runtime.invoke(second_agent, "second input", attempt_number=2)

    first_call, second_call = client.models.generate_content.await_args_list
    assert first_output.result == "first"
    assert second_output.result == "second"
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


@pytest.mark.parametrize(
    ("response_text", "defect_name"),
    [
        ("MODEL_OUTPUT_SENTINEL_NOT_JSON", "RESPONSE_NOT_JSON"),
        (json.dumps(["MODEL_OUTPUT_SENTINEL_NOT_OBJECT"]), "RESPONSE_NOT_OBJECT"),
    ],
)
async def test_invalid_response_shape_maps_to_provider_neutral_defect(
    response_text: str,
    defect_name: str,
) -> None:
    contract_module = runtime_contract()
    error_type = required_attribute(contract_module, "AgentResponseInvalidError")
    defect_type = required_attribute(contract_module, "AgentResponseDefect")
    client = FakeGeminiClient([FakeResponse(text=response_text)])
    runtime = runtime_type()(client=client)

    with pytest.raises(error_type) as exc_info:
        await runtime.invoke(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value.defect is getattr(defect_type, defect_name)
    assert "MODEL_OUTPUT_SENTINEL" not in str(exc_info.value)
    assert "MODEL_OUTPUT_SENTINEL" not in (exc_info.value.repair_hint or "")
    assert client.models.generate_content.await_count == 1


async def test_output_validation_error_exposes_only_safe_repair_fields() -> None:
    contract_module = runtime_contract()
    error_type = required_attribute(contract_module, "AgentResponseInvalidError")
    defect_type = required_attribute(contract_module, "AgentResponseDefect")
    model_output_sentinel = "MODEL_OUTPUT_SENTINEL_SECRET_4e91"
    payload = {
        "score": 0,
        "secret_number": model_output_sentinel,
        "unsafe": "trigger validator",
    }
    client = FakeGeminiClient([FakeResponse(text=json.dumps(payload))])
    runtime = runtime_type()(client=client)

    with pytest.raises(error_type) as exc_info:
        await runtime.invoke(
            make_agent(output_type=ValidationProbeOutput),
            "typed input",
            attempt_number=1,
        )

    error = exc_info.value
    safe_error_text = f"{error}\n{error.repair_hint or ''}"
    assert error.defect is defect_type.OUTPUT_SCHEMA_MISMATCH
    assert "score" in safe_error_text
    assert "greater_than_equal" in safe_error_text
    assert "ge" in safe_error_text
    assert "secret_number" in safe_error_text
    assert "int_parsing" in safe_error_text
    assert "unsafe" in safe_error_text
    assert "value_error" in safe_error_text
    assert model_output_sentinel not in safe_error_text
    assert "ARBITRARY_CTX_SENTINEL_7c62" not in safe_error_text
    assert "Input should be" not in safe_error_text
    assert "errors.pydantic.dev" not in safe_error_text


async def test_unknown_extra_field_location_is_collapsed_to_fixed_placeholder() -> None:
    contract_module = runtime_contract()
    error_type = required_attribute(contract_module, "AgentResponseInvalidError")
    defect_type = required_attribute(contract_module, "AgentResponseDefect")
    unknown_field_sentinels = (
        "MODEL_OUTPUT_UNKNOWN_KEY_SENTINEL_4b1e",
        "MODEL_OUTPUT_DIFFERENT_KEY_SENTINEL_9f73",
    )
    client = FakeGeminiClient(
        [
            FakeResponse(
                text=json.dumps(
                    {
                        "result": "accepted",
                        "tags": ["runtime"],
                        sentinel: "unknown extra value",
                    }
                )
            )
            for sentinel in unknown_field_sentinels
        ]
    )
    runtime = runtime_type()(client=client)
    errors = []

    for attempt_number in (1, 2):
        with pytest.raises(error_type) as exc_info:
            await runtime.invoke(
                make_agent(),
                "typed input",
                attempt_number=attempt_number,
            )
        errors.append(exc_info.value)

    safe_error_text = "\n".join(str(error) for error in errors)
    assert all(error.defect is defect_type.OUTPUT_SCHEMA_MISMATCH for error in errors)
    assert errors[0].repair_hint
    assert errors[0].repair_hint == errors[1].repair_hint
    assert "extra_forbidden" in errors[0].repair_hint
    assert all(sentinel not in safe_error_text for sentinel in unknown_field_sentinels)


@pytest.mark.parametrize(
    ("finish_reason", "expected_reason"),
    [
        ("SAFETY", GeminiContentRejectionReason.SAFETY),
        ("RECITATION", GeminiContentRejectionReason.RECITATION),
    ],
)
async def test_blocked_finish_reason_maps_to_existing_provider_error(
    finish_reason: str,
    expected_reason: GeminiContentRejectionReason,
) -> None:
    client = FakeGeminiClient([blocked_response(finish_reason)])
    runtime = runtime_type()(client=client)

    with pytest.raises(AIProviderOutputBlockedError) as exc_info:
        await runtime.invoke(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value.reason is expected_reason
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_known_gemini_failure_uses_existing_error_translation() -> None:
    client = FakeGeminiClient([TimeoutError("PROVIDER_SENTINEL_TIMEOUT_79ab")])
    runtime = runtime_type()(client=client)

    with pytest.raises(AIProviderNetworkError):
        await runtime.invoke(make_agent(), "typed input", attempt_number=1)

    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_unclassified_exception_propagates_with_identity() -> None:
    error = RuntimeError("UNCLASSIFIED_EXCEPTION_SENTINEL_2c5e")
    client = FakeGeminiClient([error])
    runtime = runtime_type()(client=client)

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.invoke(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value is error
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


@pytest.mark.parametrize("attempt_number", [0, -1])
async def test_non_positive_attempt_number_is_rejected_before_provider_call(
    attempt_number: int,
) -> None:
    client = FakeGeminiClient([success_response()])
    runtime = runtime_type()(client=client)

    with pytest.raises(ValueError):
        await runtime.invoke(
            make_agent(),
            "typed input",
            attempt_number=attempt_number,
        )

    client.models.generate_content.assert_not_awaited()


async def test_renderer_failure_propagates_without_provider_call() -> None:
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
    runtime = runtime_type()(client=client)

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.invoke(agent, "typed input", attempt_number=1)

    assert exc_info.value is error
    client.models.generate_content.assert_not_awaited()


async def test_config_construction_failure_happens_before_provider_call() -> None:
    client = FakeGeminiClient([success_response()])
    agent = make_agent()
    agent = replace(
        agent,
        model_settings=type(agent.model_settings)(
            temperature=SimpleNamespace(invalid="temperature"),
            max_output_tokens=321,
        ),
    )
    runtime = runtime_type()(client=client)

    with pytest.raises((TypeError, ValueError)):
        await runtime.invoke(agent, "typed input", attempt_number=1)

    client.models.generate_content.assert_not_awaited()
