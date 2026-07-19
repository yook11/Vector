"""Provider-neutral AgentRuntime contract tests."""

from __future__ import annotations

import subprocess
import sys
from inspect import Parameter, iscoroutinefunction, signature
from pathlib import Path

from tests.agent.runtime._helpers import required_attribute, runtime_contract


def _annotation_name(annotation: object) -> str:
    return getattr(annotation, "__name__", str(annotation).strip("'"))


def test_agent_runtime_protocol_has_one_attempt_generic_invoke_signature() -> None:
    """Runtimeがprovider非依存の非同期1-attempt境界として、
    型付き署名を持つことを検証する。
    """
    module = runtime_contract()
    runtime_protocol = required_attribute(module, "AgentRuntime")
    invoke = runtime_protocol.invoke
    parameters = signature(invoke).parameters

    assert getattr(runtime_protocol, "_is_protocol", False)
    assert iscoroutinefunction(invoke)
    assert list(parameters) == ["self", "agent", "input", "attempt_number"]
    assert parameters["attempt_number"].kind is Parameter.KEYWORD_ONLY
    assert parameters["attempt_number"].default is Parameter.empty
    assert "Agent" in _annotation_name(parameters["agent"].annotation)
    assert "InputT" in _annotation_name(parameters["input"].annotation)
    assert "OutputT" in _annotation_name(signature(invoke).return_annotation)


def test_agent_runtime_scope_factory_is_provider_neutral_protocol() -> None:
    """scope factory が provider 非依存の runtime 境界を返すことを守る。"""
    module = runtime_contract()
    scope_factory = required_attribute(module, "AgentRuntimeScopeFactory")
    call_signature = signature(scope_factory.__call__)

    assert getattr(scope_factory, "_is_protocol", False)
    assert list(call_signature.parameters) == ["self"]
    assert "AbstractAsyncContextManager" in _annotation_name(
        call_signature.return_annotation
    )
    assert "AgentRuntime" in _annotation_name(call_signature.return_annotation)


def test_agent_response_defect_has_only_three_provider_neutral_values() -> None:
    """応答不備の公開語彙を三つの中立値に限定する。"""
    module = runtime_contract()
    defect_type = required_attribute(module, "AgentResponseDefect")

    assert [defect.value for defect in defect_type] == [
        "response_not_json",
        "response_not_object",
        "output_schema_mismatch",
    ]


def test_agent_response_invalid_error_string_uses_defect_and_repair_hint() -> None:
    """安全な修正情報を defect と repair hint で利用可能にする。"""
    module = runtime_contract()
    defect_type = required_attribute(module, "AgentResponseDefect")
    error_type = required_attribute(module, "AgentResponseInvalidError")
    repair_hint = "field=score type=greater_than_equal ge=1"
    error = error_type(
        defect_type.OUTPUT_SCHEMA_MISMATCH,
        repair_hint=repair_hint,
    )

    assert error.defect is defect_type.OUTPUT_SCHEMA_MISMATCH
    assert error.repair_hint == repair_hint
    assert defect_type.OUTPUT_SCHEMA_MISMATCH.value in str(error)
    assert repair_hint in str(error)


def test_contract_import_does_not_eagerly_import_gemini_runtime() -> None:
    """中立 contract の import が Gemini 依存を導入しないことを守る。"""
    backend_dir = Path(__file__).resolve().parents[3]
    script = """
import sys
import app.agent.runtime.contract

raise SystemExit(int("app.agent.runtime.gemini" in sys.modules))
"""

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
