"""Agentの出力の検証。providerが返したJSON objectを直接渡す(外部依存なし)。"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from app.agent.output import (
    AgentOutputDefect,
    AgentOutputInvalidError,
    validate_output,
)
from app.shared.errors import ApplicationError
from tests.agent.llm_call._helpers import (
    CallerOutput,
    ValidationProbeOutput,
    make_agent,
)


@dataclass(frozen=True, slots=True)
class DataclassCallerOutput:
    result: str
    tags: list[str]


def test_agent_output_defect_has_only_the_schema_mismatch_value() -> None:
    """出力の契約違反の公開語彙は、出力の型に合わないことの1値に限る。"""
    assert [defect.value for defect in AgentOutputDefect] == [
        "output_schema_mismatch",
    ]


def test_agent_output_invalid_error_string_uses_defect_and_repair_hint() -> None:
    """安全な修正情報を defect と repair hint で利用可能にする。"""
    repair_hint = "field=score type=greater_than_equal ge=1"
    error = AgentOutputInvalidError(
        AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH,
        repair_hint=repair_hint,
    )

    assert error.defect is AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH
    assert error.repair_hint == repair_hint
    assert AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH.value in str(error)
    assert repair_hint in str(error)


def test_agent_output_invalid_error_is_application_error_with_defect_details() -> None:
    """出力の契約違反はアプリの例外として、種類をログに出せる診断情報に持つ。"""
    error = AgentOutputInvalidError(
        AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH,
        repair_hint="field=score type=greater_than_equal ge=1",
    )

    assert isinstance(error, ApplicationError)
    assert error.details == {"defect": "output_schema_mismatch"}


def test_validate_output_returns_the_declared_model() -> None:
    """宣言された出力の型に合うJSON objectは、その型の値になる。"""
    output = validate_output(make_agent(), {"result": "validated", "tags": ["caller"]})

    assert output == CallerOutput(result="validated", tags=["caller"])


def test_validate_output_returns_a_declared_dataclass() -> None:
    """宣言された dataclass の出力の型でも検証して値にする。"""
    agent = replace(make_agent(), output_type=DataclassCallerOutput)

    output = validate_output(agent, {"result": "dataclass", "tags": ["caller"]})

    assert output == DataclassCallerOutput(result="dataclass", tags=["caller"])


def test_output_validation_error_exposes_only_safe_repair_fields() -> None:
    """出力検証エラーから安全な修正情報だけを公開する。"""
    model_output_sentinel = "MODEL_OUTPUT_SENTINEL_SECRET_4e91"
    payload = {
        "score": 0,
        "secret_number": model_output_sentinel,
        "unsafe": "trigger validator",
    }

    with pytest.raises(AgentOutputInvalidError) as exc_info:
        validate_output(make_agent(output_type=ValidationProbeOutput), payload)

    error = exc_info.value
    safe_error_text = f"{error}\n{error.repair_hint or ''}"
    assert error.defect is AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH
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
    assert error.__context__ is None
    assert error.__cause__ is None


def test_unknown_extra_field_location_is_collapsed_to_fixed_placeholder() -> None:
    """未知の追加 field 名を固定の安全な表現へ畳み込む。"""
    unknown_field_sentinels = (
        "MODEL_OUTPUT_UNKNOWN_KEY_SENTINEL_4b1e",
        "MODEL_OUTPUT_DIFFERENT_KEY_SENTINEL_9f73",
    )
    errors = []

    for sentinel in unknown_field_sentinels:
        with pytest.raises(AgentOutputInvalidError) as exc_info:
            validate_output(
                make_agent(),
                {
                    "result": "accepted",
                    "tags": ["caller"],
                    sentinel: "unknown extra value",
                },
            )
        errors.append(exc_info.value)

    safe_error_text = "\n".join(str(error) for error in errors)
    assert all(
        error.defect is AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH for error in errors
    )
    assert errors[0].repair_hint
    assert errors[0].repair_hint == errors[1].repair_hint
    assert "extra_forbidden" in errors[0].repair_hint
    assert all(sentinel not in safe_error_text for sentinel in unknown_field_sentinels)
