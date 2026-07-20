"""Input Safety Service の一試行・正規化・可観測性契約。"""

from __future__ import annotations

import inspect
import json
from enum import StrEnum
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

import app.analysis.ai_provider_errors as ai_provider_errors
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from tests.agent.input_safety._helpers import (
    RecordingRuntimeScopeFactory,
    required_input_safety_attribute,
    required_input_safety_module,
)
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics

_OUTCOME_METRIC = "vector.agent.input_safety.outcome"
_RUN_ID = UUID("00000000-0000-4000-a000-000000000021")


class _ProviderNeutralContentReason(StrEnum):
    FILTERED = "filtered"


def _attribute(module_name: str, name: str) -> object:
    return required_input_safety_attribute(
        required_input_safety_module(module_name), name
    )


def _outcome_metrics(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    return [
        data_point.get("attributes", {})
        for metric in collected_metrics(capfire)
        if metric["name"] == _OUTCOME_METRIC
        for data_point in metric["data"]["data_points"]
    ]


def _service(
    outcomes: list[object | BaseException],
) -> tuple[object, ScriptedAgentRuntime, RecordingRuntimeScopeFactory]:
    agent = _attribute("agent", "INPUT_SAFETY_AGENT")
    service_type = _attribute("service", "InputSafetyService")
    runtime = ScriptedAgentRuntime(outcomes)
    factory = RecordingRuntimeScopeFactory(runtime)
    return (
        service_type(agent=agent, runtime_scope_factory=factory),  # type: ignore[operator]
        runtime,
        factory,
    )


async def test_allow_uses_one_scope_and_one_attempt_without_audit_log(
    capfire: CaptureLogfire,
) -> None:
    output_type = _attribute("contract", "InputSafetyAgentOutput")
    input_type = _attribute("contract", "InputSafetyAgentInput")
    previous_turn_type = _attribute("contract", "InputSafetyPreviousTurn")
    service, runtime, factory = _service(
        [
            output_type(  # type: ignore[operator]
                input_safety_result="allow",
                block_reason=None,
            )
        ]
    )
    previous_turn = previous_turn_type(  # type: ignore[operator]
        user_question="previous question",
        assistant_answer="previous answer",
    )

    with capture_logs() as logs:
        result = await service.check(  # type: ignore[attr-defined]
            question="current question",
            previous_turn=previous_turn,
            run_id=_RUN_ID,
        )

    assert result.model_dump() == {
        "input_safety_result": "allow",
        "block_reason": None,
    }
    assert (factory.created, factory.entered, len(factory.exits)) == (1, 1, 1)
    assert len(runtime.calls) == 1
    assert runtime.calls[0].attempt_number == 1
    assert runtime.calls[0].input == input_type(  # type: ignore[operator]
        question="current question",
        previous_turn=previous_turn,
    )
    assert logs == []
    assert _outcome_metrics(capfire) == [{"result": "allow", "block_reason": "none"}]


async def test_classifier_block_passes_the_single_reason_through_to_check_result(
    capfire: CaptureLogfire,
) -> None:
    output_type = _attribute("contract", "InputSafetyAgentOutput")
    reason_type = _attribute("contract", "InputSafetyBlockReason")
    classifier_reason = reason_type.SELF_HARM_INSTRUCTIONS  # type: ignore[union-attr]
    classifier_output = output_type(  # type: ignore[operator]
        input_safety_result="block",
        block_reason=classifier_reason,
    )
    service, runtime, factory = _service([classifier_output])
    question = "QUESTION_SENTINEL_POLICY_BLOCK_87e2"
    previous_turn_type = _attribute("contract", "InputSafetyPreviousTurn")
    previous_turn = previous_turn_type(  # type: ignore[operator]
        user_question="PREVIOUS_SENTINEL_POLICY_BLOCK_b7f1",
        assistant_answer="ASSISTANT_SENTINEL_POLICY_BLOCK_ca21",
    )

    with capture_logs() as logs:
        result = await service.check(  # type: ignore[attr-defined]
            question=question,
            previous_turn=previous_turn,
            run_id=_RUN_ID,
        )

    blocked_logs = [
        entry for entry in logs if entry.get("event") == "agent_input_safety_blocked"
    ]
    assert result.model_dump() == {
        "input_safety_result": "block",
        "block_reason": "self_harm_instructions",
    }
    assert classifier_output.block_reason is classifier_reason
    assert result.block_reason is classifier_reason
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert (factory.created, factory.entered, len(factory.exits)) == (1, 1, 1)
    assert len(blocked_logs) == 1
    assert {
        key: blocked_logs[0][key]
        for key in (
            "run_id",
            "block_reason",
            "ai_model",
            "prompt_version",
            "input_length",
        )
    } == {
        "run_id": str(_RUN_ID),
        "block_reason": "self_harm_instructions",
        "ai_model": "gemini-2.5-flash-lite",
        "prompt_version": _attribute("agent", "INPUT_SAFETY_AGENT").prompt.version,
        "input_length": len(question),
    }
    assert _outcome_metrics(capfire) == [
        {"result": "block", "block_reason": "self_harm_instructions"}
    ]
    observed = json.dumps([logs, _outcome_metrics(capfire)], default=str)
    assert question not in observed
    assert "PREVIOUS_SENTINEL_POLICY_BLOCK_b7f1" not in observed
    assert "ASSISTANT_SENTINEL_POLICY_BLOCK_ca21" not in observed


@pytest.mark.parametrize(
    "error_type",
    [
        pytest.param(AIProviderInputRejectedError, id="input-blocked"),
        pytest.param(AIProviderOutputBlockedError, id="output-blocked"),
    ],
)
async def test_only_provider_safety_errors_normalize_to_provider_filter_block(
    error_type: type[AIProviderContentError],
    capfire: CaptureLogfire,
) -> None:
    kind_type = getattr(
        ai_provider_errors,
        "AIProviderContentRejectionKind",
        None,
    )
    assert kind_type is not None
    error = error_type(
        reason=_ProviderNeutralContentReason.FILTERED,
        rejection_kind=kind_type.SAFETY,
    )
    service, runtime, _factory = _service([error])

    result = await service.check(  # type: ignore[attr-defined]
        question="provider safety mapping",
        previous_turn=None,
        run_id=_RUN_ID,
    )

    assert result.model_dump() == {
        "input_safety_result": "block",
        "block_reason": "provider_safety_filter",
    }
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert _outcome_metrics(capfire) == [
        {"result": "block", "block_reason": "provider_safety_filter"}
    ]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            AIProviderInputRejectedError(
                reason=GeminiContentRejectionReason.INPUT_BLOCKED
            ),
            id="generic-input-with-input-blocked-reason",
        ),
        pytest.param(
            AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
            id="generic-output-with-safety-reason",
        ),
        pytest.param(
            AIProviderInputRejectedError(
                reason=GeminiContentRejectionReason.CONTEXT_LENGTH
            ),
            id="other-input-rejection",
        ),
        pytest.param(
            AIProviderOutputBlockedError(
                reason=GeminiContentRejectionReason.RECITATION
            ),
            id="recitation",
        ),
        pytest.param(
            AgentResponseInvalidError(
                AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH,
                repair_hint="RAW_RESPONSE_SENTINEL_fa09",
            ),
            id="invalid-agent-output",
        ),
    ],
)
async def test_non_safety_failures_propagate_once_with_pii_free_operational_log(
    error: BaseException,
    capfire: CaptureLogfire,
) -> None:
    service, runtime, factory = _service([error])
    question = "QUESTION_SENTINEL_FAILURE_7df2"
    previous_turn_type = _attribute("contract", "InputSafetyPreviousTurn")
    previous_turn = previous_turn_type(  # type: ignore[operator]
        user_question="PREVIOUS_SENTINEL_FAILURE_89a1",
        assistant_answer=None,
    )

    with capture_logs() as logs:
        with pytest.raises(type(error)) as raised:
            await service.check(  # type: ignore[attr-defined]
                question=question,
                previous_turn=previous_turn,
                run_id=_RUN_ID,
            )

    failure_logs = [
        entry for entry in logs if entry.get("event") == "agent_input_safety_failed"
    ]
    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert len(factory.exits) == 1 and factory.exits[0][1] is error
    assert len(failure_logs) == 1
    assert _outcome_metrics(capfire) == [{"result": "failed", "block_reason": "none"}]
    observed = json.dumps([logs, _outcome_metrics(capfire)], default=str)
    assert question not in observed
    assert "PREVIOUS_SENTINEL_FAILURE_89a1" not in observed
    assert "RAW_RESPONSE_SENTINEL_fa09" not in observed


def test_service_module_does_not_depend_on_gemini_rejection_vocabulary() -> None:
    source = inspect.getsource(required_input_safety_module("service"))

    assert "gemini_error_translator" not in source
    assert "GeminiContentRejectionReason" not in source
