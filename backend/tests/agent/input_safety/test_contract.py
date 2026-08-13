"""Input Safety のwire/application contract tests。"""

from __future__ import annotations

from inspect import signature

import pytest
from pydantic import ValidationError

import app.agent.input_safety.contract as input_safety_contract
from app.agent.input_safety.contract import (
    InputSafetyAgentOutput,
    InputSafetyBlocked,
    InputSafetyBlockReason,
    InputSafetyCheckResult,
)


def test_agent_output_uses_the_single_reason_type_for_five_policy_reasons() -> None:
    policy_reason_values = [
        "dangerous_or_illegal_instructions",
        "credential_or_privacy_abuse",
        "targeted_hate_or_harassment",
        "sexual_exploitation",
        "self_harm_instructions",
    ]

    allow = InputSafetyAgentOutput.model_validate(
        {"input_safety_result": "allow", "block_reason": None}
    )
    blocked = [
        InputSafetyAgentOutput.model_validate(
            {"input_safety_result": "block", "block_reason": reason}
        )
        for reason in policy_reason_values
    ]

    assert allow.model_dump() == {
        "input_safety_result": "allow",
        "block_reason": None,
    }
    assert [item.block_reason.value for item in blocked] == policy_reason_values
    assert all(item.block_reason in InputSafetyBlockReason for item in blocked)
    assert not hasattr(input_safety_contract, "InputSafetyAgentBlockReason")


@pytest.mark.parametrize(
    "payload",
    [
        {"input_safety_result": "allow", "block_reason": "self_harm_instructions"},
        {"input_safety_result": "block", "block_reason": None},
        {"input_safety_result": "unknown", "block_reason": None},
        {"input_safety_result": "block", "block_reason": "unknown"},
        {
            "input_safety_result": "block",
            "block_reason": "provider_safety_filter",
        },
        {"input_safety_result": "allow", "block_reason": None, "extra": True},
    ],
)
def test_agent_output_rejects_invalid_or_provider_only_wire_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        InputSafetyAgentOutput.model_validate(payload)


def test_check_result_accepts_all_six_input_safety_block_reasons() -> None:
    reason_values = [reason.value for reason in InputSafetyBlockReason]

    allow = InputSafetyCheckResult.model_validate(
        {"input_safety_result": "allow", "block_reason": None}
    )
    blocked = [
        InputSafetyCheckResult.model_validate(
            {"input_safety_result": "block", "block_reason": reason}
        )
        for reason in reason_values
    ]

    assert allow.is_blocked is False
    assert reason_values == [
        "dangerous_or_illegal_instructions",
        "credential_or_privacy_abuse",
        "targeted_hate_or_harassment",
        "sexual_exploitation",
        "self_harm_instructions",
        "provider_safety_filter",
    ]
    assert all(item.is_blocked is True for item in blocked)
    assert all("is_blocked" not in item.model_dump() for item in blocked)
    assert [item.block_reason.value for item in blocked] == reason_values


@pytest.mark.parametrize(
    "payload",
    [
        {"input_safety_result": "allow", "block_reason": "provider_safety_filter"},
        {"input_safety_result": "block", "block_reason": None},
        {"input_safety_result": "block", "block_reason": "unknown"},
        {"input_safety_result": "allow", "block_reason": None, "extra": True},
    ],
)
def test_application_result_keeps_the_same_strict_combination_boundary(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        InputSafetyCheckResult.model_validate(payload)


def test_input_safety_blocked_carries_only_a_typed_reason() -> None:
    reason = InputSafetyBlockReason.SELF_HARM_INSTRUCTIONS
    error = InputSafetyBlocked(block_reason=reason)

    assert error.block_reason is reason
    assert vars(error) == {"block_reason": reason}
    assert list(signature(InputSafetyBlocked).parameters) == ["block_reason"]
