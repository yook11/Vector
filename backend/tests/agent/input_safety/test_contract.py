"""Input Safety のwire/application contract tests。"""

from __future__ import annotations

from inspect import signature

import pytest
from pydantic import ValidationError

from tests.agent.input_safety._helpers import (
    required_input_safety_attribute,
    required_input_safety_module,
)


def _contract() -> object:
    return required_input_safety_module("contract")


def _attribute(name: str) -> object:
    return required_input_safety_attribute(_contract(), name)


def test_agent_output_uses_the_single_reason_type_for_five_policy_reasons() -> None:
    agent_output_type = _attribute("InputSafetyAgentOutput")
    reason_type = _attribute("InputSafetyBlockReason")
    policy_reason_values = [
        "dangerous_or_illegal_instructions",
        "credential_or_privacy_abuse",
        "targeted_hate_or_harassment",
        "sexual_exploitation",
        "self_harm_instructions",
    ]

    allow = agent_output_type.model_validate(  # type: ignore[attr-defined]
        {"input_safety_result": "allow", "block_reason": None}
    )
    blocked = [
        agent_output_type.model_validate(  # type: ignore[attr-defined]
            {"input_safety_result": "block", "block_reason": reason}
        )
        for reason in policy_reason_values
    ]

    assert allow.model_dump() == {
        "input_safety_result": "allow",
        "block_reason": None,
    }
    assert [item.block_reason.value for item in blocked] == policy_reason_values
    assert all(item.block_reason in reason_type for item in blocked)  # type: ignore[operator]
    assert not hasattr(_contract(), "InputSafetyAgentBlockReason")


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
    agent_output_type = _attribute("InputSafetyAgentOutput")

    with pytest.raises(ValidationError):
        agent_output_type.model_validate(payload)  # type: ignore[attr-defined]


def test_check_result_accepts_all_six_input_safety_block_reasons() -> None:
    result_type = _attribute("InputSafetyCheckResult")
    reason_type = _attribute("InputSafetyBlockReason")
    reason_values = [reason.value for reason in reason_type]  # type: ignore[union-attr]

    allow = result_type.model_validate(  # type: ignore[attr-defined]
        {"input_safety_result": "allow", "block_reason": None}
    )
    blocked = [
        result_type.model_validate(  # type: ignore[attr-defined]
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
    result_type = _attribute("InputSafetyCheckResult")

    with pytest.raises(ValidationError):
        result_type.model_validate(payload)  # type: ignore[attr-defined]


def test_input_safety_blocked_carries_only_a_typed_reason() -> None:
    blocked_type = _attribute("InputSafetyBlocked")
    reason_type = _attribute("InputSafetyBlockReason")
    reason = reason_type.SELF_HARM_INSTRUCTIONS  # type: ignore[union-attr]
    error = blocked_type(block_reason=reason)  # type: ignore[operator]

    assert error.block_reason is reason
    assert vars(error) == {"block_reason": reason}
    assert list(signature(blocked_type).parameters) == ["block_reason"]  # type: ignore[arg-type]
