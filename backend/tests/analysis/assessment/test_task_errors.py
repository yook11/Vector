"""既存Taskiq向けの分類属性とServiceエラーからの変換を検証する。"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderRateLimitedError,
)
from app.analysis.assessment.ai.deepseek import DeepSeekResponseDefect
from app.analysis.assessment.ai.gemini import GeminiResponseDefect
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.errors import (
    AssessmentCurationMissingError,
    AssessmentResponseInvalidError,
)
from app.analysis.assessment.task_errors import (
    AssessmentRecoverableError,
    AssessmentTaskError,
    AssessmentTerminalError,
    to_assessment_task_error,
)
from app.audit.failure_projection import Retryability
from app.db.errors import DatabaseConnectionError, DatabaseConnectionErrorReason

# Layer 1 の 2 marker は同形 (retry 軸だけ classvar で違い、原因軸は instance 値)。
_LAYER1_MARKERS = (AssessmentRecoverableError, AssessmentTerminalError)


class TestAssessmentRecoverableError:
    """``AssessmentRecoverableError`` の constructor / instance attr 振る舞い。"""

    def test_holds_cause_axis_and_provider_error(self) -> None:
        original = AIProviderRateLimitedError()
        exc = AssessmentRecoverableError(
            code="ai_error_rate_limited",
            failure_kind="time_based_recovery",
            failure_reason="rate_limited",
            provider_error=original,
        )

        assert exc.code == "ai_error_rate_limited"
        assert exc.failure_kind == "time_based_recovery"
        assert exc.failure_reason == "rate_limited"
        assert exc.provider_error is original

    def test_optional_attrs_default(self) -> None:
        exc = AssessmentRecoverableError(
            code="assessment_response_invalid",
            failure_kind="ai_response_invalid",
        )

        assert exc.failure_reason is None
        assert exc.provider_error is None

    def test_str_renders_code_only(self) -> None:
        # SAFE_ATTRS=("code",): failure_kind / failure_reason は span に載せない。
        exc = AssessmentRecoverableError(
            code="ai_error_rate_limited",
            failure_kind="time_based_recovery",
            failure_reason="rate_limited",
        )
        assert str(exc) == "AssessmentRecoverableError(code='ai_error_rate_limited')"

    def test_code_and_failure_kind_are_required(self) -> None:
        with pytest.raises(TypeError):
            AssessmentRecoverableError(code="x")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            AssessmentRecoverableError(failure_kind="x")  # type: ignore[call-arg]

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            AssessmentRecoverableError("msg")  # type: ignore[call-arg]


class TestAssessmentTerminalError:
    """``AssessmentTerminalError`` は具象 (旧 abstract / subclass 強制は撤去)。"""

    def test_is_concrete_and_holds_cause_axis(self) -> None:
        original = AIProviderConfigurationError()
        exc = AssessmentTerminalError(
            code="ai_error_configuration",
            failure_kind="operator_action_required",
            provider_error=original,
        )

        assert exc.code == "ai_error_configuration"
        assert exc.failure_kind == "operator_action_required"
        assert exc.failure_reason is None
        assert exc.provider_error is original

    def test_str_renders_code_only(self) -> None:
        exc = AssessmentTerminalError(
            code="ai_error_input_rejected",
            failure_kind="target_rejected",
            failure_reason="safety",
        )
        assert str(exc) == "AssessmentTerminalError(code='ai_error_input_rejected')"

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            AssessmentTerminalError("msg")  # type: ignore[call-arg]


class TestStage4MarkerHierarchy:
    """Stage 4 marker の型階層 (retry 軸 = Recoverable / Terminal の 2 本)。"""

    @pytest.mark.parametrize("marker", _LAYER1_MARKERS)
    def test_layer1_subclasses_assessment_error(
        self, marker: type[AssessmentTaskError]
    ) -> None:
        assert issubclass(marker, AssessmentTaskError)

    def test_recoverable_and_terminal_are_disjoint(self) -> None:
        assert not issubclass(AssessmentRecoverableError, AssessmentTerminalError)
        assert not issubclass(AssessmentTerminalError, AssessmentRecoverableError)

    def test_assessment_error_is_exception(self) -> None:
        assert issubclass(AssessmentTaskError, Exception)

    def test_marker_classvars_are_audit_projection_contract(self) -> None:
        # retry 軸だけ型で固定 (原因軸 failure_kind は instance 値、本テスト対象外)。
        assert not hasattr(AssessmentTaskError, "STAGE")
        assert AssessmentRecoverableError.RETRYABILITY is Retryability.RETRYABLE
        assert AssessmentTerminalError.RETRYABILITY is Retryability.NON_RETRYABLE
        assert AssessmentRecoverableError.FAILURE_ACTION is None
        assert AssessmentTerminalError.FAILURE_ACTION is None


@pytest.mark.parametrize(
    "defect",
    [*AssessmentResponseDefect, *GeminiResponseDefect, *DeepSeekResponseDefect],
)
def test_response_defect_survives_task_conversion(defect):
    original = AssessmentResponseInvalidError(defect)
    result = to_assessment_task_error(original)
    assert isinstance(result, AssessmentRecoverableError)
    assert result.code == defect.value
    assert result.failure_kind == "ai_response_invalid"
    assert result.failure_reason is None
    assert result.provider_error is None
    assert result.__cause__ is original


def test_missing_curation_converts_to_terminal():
    original = AssessmentCurationMissingError()
    result = to_assessment_task_error(original)
    assert isinstance(result, AssessmentTerminalError)
    assert result.code == "assessment_curation_missing"
    assert result.failure_kind == "target_missing"
    assert result.failure_reason is None
    assert result.provider_error is None
    assert result.__cause__ is original


@pytest.mark.parametrize(
    "original",
    [
        DatabaseConnectionError(reason=DatabaseConnectionErrorReason.CONNECTION_FAILED),
        TimeoutError("timeout"),
        RuntimeError("unexpected"),
        AssessmentRecoverableError(
            code="ai_error_network", failure_kind="attempt_scoped"
        ),
    ],
)
def test_non_assessment_errors_pass_through_unchanged(original):
    assert to_assessment_task_error(original) is original
    assert original.__cause__ is None
