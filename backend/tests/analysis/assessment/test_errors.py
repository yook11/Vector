"""Stage 4 (Assessment) Layer 1 / Layer 2-B marker の振る舞いテスト。

Layer 1 marker は retry 軸 (``RETRYABILITY``) だけを型で固定し、原因軸
(``failure_kind`` = 回復クラス / ``failure_reason`` = 詳細) は instance 値で持つ。
``Recoverable`` / ``Terminal`` はどちらも具象で同形の kwargs-only constructor。
hold (stage 退避) は marker 型ではなく handler が provider mode から導出するため、
旧 ``*StageBlocked`` / ``*TargetRejected`` は存在しない。
``__str__`` は SAFE_ATTRS=("code",) のみ (``failure_reason`` は forensic で非露出)。
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderRateLimitedError,
)
from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalError,
)
from app.audit.domain.event import Stage
from app.audit.failure_projection import Retryability

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
        self, marker: type[AssessmentError]
    ) -> None:
        assert issubclass(marker, AssessmentError)

    def test_recoverable_and_terminal_are_disjoint(self) -> None:
        assert not issubclass(AssessmentRecoverableError, AssessmentTerminalError)
        assert not issubclass(AssessmentTerminalError, AssessmentRecoverableError)

    def test_assessment_error_is_exception(self) -> None:
        assert issubclass(AssessmentError, Exception)

    def test_retry_axis_classvars_are_audit_projection_ssot(self) -> None:
        # retry 軸だけ型で固定 (原因軸 failure_kind は instance 値、本テスト対象外)。
        assert AssessmentError.STAGE is Stage.ASSESSMENT
        assert AssessmentRecoverableError.RETRYABILITY is Retryability.RETRYABLE
        assert AssessmentTerminalError.RETRYABILITY is Retryability.NON_RETRYABLE
        assert AssessmentRecoverableError.FAILURE_ACTION is None
        assert AssessmentTerminalError.FAILURE_ACTION is None


# Layer 2-B marker (Stage 4 工程由来 / provider_error=None 固定)


class _SampleDefect(StrEnum):
    """型ガード契約を検証するためのローカル defect (検知場所の enum を模す)。"""

    SOMETHING = "sample_something"


class TestAssessmentResponseInvalidError:
    """marker ``AssessmentResponseInvalidError`` (Recoverable 系、defect 載せ替え)。

    marker は検知場所の enum を import せず ``StrEnum`` で受ける。code は渡された
    defect の value、原因ファミリーは provider 起因でないため
    ``failure_kind="ai_response_invalid"`` 固定。型ガードで非 StrEnum を拒否する
    (PII 境界) ことを isolation で確認する。
    """

    def test_is_recoverable_subclass(self) -> None:
        assert issubclass(AssessmentResponseInvalidError, AssessmentRecoverableError)

    def test_code_is_defect_value(self) -> None:
        exc = AssessmentResponseInvalidError(_SampleDefect.SOMETHING)
        assert exc.code == "sample_something"

    def test_failure_kind_is_ai_response_invalid(self) -> None:
        exc = AssessmentResponseInvalidError(_SampleDefect.SOMETHING)
        assert exc.failure_kind == "ai_response_invalid"

    def test_provider_error_and_reason_are_none(self) -> None:
        exc = AssessmentResponseInvalidError(_SampleDefect.SOMETHING)
        assert exc.provider_error is None
        assert exc.failure_reason is None

    def test_str_renders_code_only(self) -> None:
        exc = AssessmentResponseInvalidError(_SampleDefect.SOMETHING)
        assert str(exc) == "AssessmentResponseInvalidError(code='sample_something')"

    def test_non_strenum_defect_rejected(self) -> None:
        with pytest.raises(TypeError):
            AssessmentResponseInvalidError("schema mismatch")  # type: ignore[arg-type]

    def test_response_invalid_not_terminal(self) -> None:
        assert not issubclass(AssessmentResponseInvalidError, AssessmentTerminalError)
