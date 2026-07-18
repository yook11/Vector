"""DeepSeekAgentRuntime のone-attempt structured-output契約。"""

from __future__ import annotations

import json
from dataclasses import replace
from importlib import import_module
from inspect import signature

import pytest

from app.analysis.ai_provider_errors import AIProviderNetworkError
from tests.agent.runtime._deepseek_helpers import (
    DataclassRuntimeOutput,
    FakeDeepSeekClient,
    RuntimeOutput,
    binding_type,
    function_response,
    make_agent,
    make_binding,
    required_attribute,
    runtime_contract,
    runtime_type,
    success_response,
)


async def test_constructor_accepts_only_borrowed_client_and_output_binding() -> None:
    runtime = runtime_type()

    assert list(signature(runtime).parameters) == ["client", "binding"]
    runtime(client=FakeDeepSeekClient([success_response()]), binding=make_binding())


async def test_invoke_makes_one_forced_strict_function_call_and_returns_draft() -> None:
    client = FakeDeepSeekClient([success_response(result="validated")])
    binding = make_binding()
    runtime = runtime_type()(client=client, binding=binding)
    agent = make_agent(max_output_tokens=456)

    output = await runtime.invoke(agent, object(), attempt_number=1)

    kwargs = client.chat.completions.create.await_args.kwargs
    assert output == RuntimeOutput(result="validated", tags=["runtime"])
    assert client.chat.completions.create.await_count == 1
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["messages"] == [
        {"role": "system", "content": agent.prompt.instructions},
        {"role": "user", "content": "TASK_CONTENTS_SENTINEL_65ba"},
    ]
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": binding.function_name,
                "strict": True,
                "description": binding.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["result", "tags"],
                    "properties": {
                        "result": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        }
    ]
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": binding.function_name},
    }
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["max_tokens"] == 456
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_invoke_validates_declared_dataclass_output() -> None:
    client = FakeDeepSeekClient([success_response(result="dataclass")])
    runtime = runtime_type()(client=client, binding=make_binding())

    output = await runtime.invoke(
        replace(make_agent(), output_type=DataclassRuntimeOutput),
        object(),
        attempt_number=1,
    )

    assert output == DataclassRuntimeOutput(result="dataclass", tags=["runtime"])


@pytest.mark.parametrize(
    ("response", "defect_name"),
    [
        pytest.param(
            function_response(arguments="MODEL_OUTPUT_NOT_JSON_SENTINEL"),
            "RESPONSE_NOT_JSON",
            id="invalid-json",
        ),
        pytest.param(
            function_response(
                arguments=json.dumps(["MODEL_OUTPUT_NOT_OBJECT_SENTINEL"])
            ),
            "RESPONSE_NOT_OBJECT",
            id="non-object-json",
        ),
        pytest.param(
            function_response(no_tool_calls=True),
            "OUTPUT_SCHEMA_MISMATCH",
            id="missing-function-call",
        ),
        pytest.param(
            function_response(arguments="{}", function_name="unexpected_function"),
            "OUTPUT_SCHEMA_MISMATCH",
            id="wrong-function-name",
        ),
        pytest.param(
            function_response(arguments=json.dumps({"result": "missing tags"})),
            "OUTPUT_SCHEMA_MISMATCH",
            id="output-schema-mismatch",
        ),
    ],
)
async def test_invalid_structured_output_maps_to_three_safe_neutral_defects(
    response: object,
    defect_name: str,
) -> None:
    contract = runtime_contract()
    error_type = required_attribute(contract, "AgentResponseInvalidError")
    defect_type = required_attribute(contract, "AgentResponseDefect")
    client = FakeDeepSeekClient([response])

    with pytest.raises(error_type) as raised:
        await runtime_type()(client=client, binding=make_binding()).invoke(
            make_agent(), object(), attempt_number=1
        )

    error = raised.value
    assert error.defect is getattr(defect_type, defect_name)
    assert "MODEL_OUTPUT" not in str(error)
    assert "MODEL_OUTPUT" not in (error.repair_hint or "")
    assert error.__context__ is None
    assert error.__cause__ is None


async def test_negative_index_is_runtime_schema_mismatch() -> None:
    contract = runtime_contract()
    error_type = required_attribute(contract, "AgentResponseInvalidError")
    defect_type = required_attribute(contract, "AgentResponseDefect")
    selector_contract = import_module(
        "app.agent.evidence_collection.external_search.contract"
    )
    selector_draft = required_attribute(
        selector_contract, "ExternalEvidenceSelectionDraft"
    )
    client = FakeDeepSeekClient(
        [
            function_response(
                arguments=json.dumps(
                    {
                        "selections": [
                            {
                                "candidate_index": -1,
                                "claim": "claim",
                                "why_selected": "why",
                            }
                        ],
                        "missing": [],
                    }
                )
            )
        ]
    )
    agent = replace(make_agent(), output_type=selector_draft)

    with pytest.raises(error_type) as raised:
        await runtime_type()(client=client, binding=make_binding()).invoke(
            agent, object(), attempt_number=1
        )

    assert raised.value.defect is defect_type.OUTPUT_SCHEMA_MISMATCH


async def test_known_error_translates_and_unknown_keeps_identity() -> None:
    known_client = FakeDeepSeekClient([TimeoutError("PROVIDER_MESSAGE_SENTINEL")])
    unknown = RuntimeError("UNCLASSIFIED_SENTINEL")
    unknown_client = FakeDeepSeekClient([unknown])

    with pytest.raises(AIProviderNetworkError) as known_raised:
        await runtime_type()(client=known_client, binding=make_binding()).invoke(
            make_agent(), object(), attempt_number=1
        )
    with pytest.raises(RuntimeError) as raised:
        await runtime_type()(client=unknown_client, binding=make_binding()).invoke(
            make_agent(), object(), attempt_number=1
        )

    assert known_raised.value.__context__ is None
    assert known_raised.value.__cause__ is None
    assert "PROVIDER_MESSAGE_SENTINEL" not in str(known_raised.value)
    assert raised.value is unknown


@pytest.mark.parametrize("attempt_number", [0, -1])
async def test_invalid_attempt_number_is_rejected_before_render_or_provider_call(
    attempt_number: int,
) -> None:
    client = FakeDeepSeekClient([success_response()])

    with pytest.raises(ValueError):
        await runtime_type()(client=client, binding=make_binding()).invoke(
            make_agent(), object(), attempt_number=attempt_number
        )

    client.chat.completions.create.assert_not_awaited()


async def test_non_deepseek_agent_is_rejected_before_provider_call() -> None:
    client = FakeDeepSeekClient([success_response()])
    agent = replace(
        make_agent(),
        model=replace(make_agent().model, provider="gemini"),
    )

    with pytest.raises(ValueError):
        await runtime_type()(client=client, binding=make_binding()).invoke(
            agent, object(), attempt_number=1
        )

    client.chat.completions.create.assert_not_awaited()


def test_output_binding_contains_only_provider_transport_identity() -> None:
    binding = make_binding()

    assert list(signature(binding_type()).parameters) == [
        "function_name",
        "description",
    ]
    assert binding.function_name == "runtime_probe_output"
    assert binding.description == "Return the declared output object."
    assert not hasattr(binding, "schema")
