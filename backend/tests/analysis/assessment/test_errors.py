"""Stage 4 (Assessment) Layer 1 / Layer 2-B marker の振る舞いテスト。

Phase 4: Layer 1 marker は kwargs-only constructor、``__str__`` は SAFE_ATTRS=
("code",) のみ。Layer 2-B (Response / Category) は no-arg constructor + 固定
code。``message`` 引数経路は廃止 (PII 隔離契約)。
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderRateLimitedError,
)
from app.analysis.assessment.errors import (
    AssessmentCategoryMissingError,
    AssessmentError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalError,
    AssessmentTerminalStageBlockedError,
    AssessmentTerminalTargetRejectedError,
)
from app.audit.domain.event import Stage
from app.audit.failure_projection import Retryability


class TestAssessmentRecoverableError:
    """``AssessmentRecoverableError`` の constructor / instance attr 振る舞い。"""

    def test_holds_code_and_provider_error(self) -> None:
        original = AIProviderRateLimitedError()
        exc = AssessmentRecoverableError(
            code="ai_error_rate_limited",
            provider_error=original,
        )

        assert exc.code == "ai_error_rate_limited"
        assert exc.provider_error is original
        assert str(exc) == "AssessmentRecoverableError(code='ai_error_rate_limited')"

    def test_provider_error_defaults_to_none(self) -> None:
        exc = AssessmentRecoverableError(code="assessment_response_invalid")

        assert exc.code == "assessment_response_invalid"
        assert exc.provider_error is None

    def test_code_is_required_kwarg(self) -> None:
        with pytest.raises(TypeError):
            AssessmentRecoverableError()  # type: ignore[call-arg]

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            AssessmentRecoverableError("msg")  # type: ignore[call-arg]


class TestAssessmentTerminalStageBlockedError:
    """``AssessmentTerminalStageBlockedError`` の constructor / attr 振る舞い。"""

    def test_holds_code_and_provider_error(self) -> None:
        original = AIProviderConfigurationError()
        exc = AssessmentTerminalStageBlockedError(
            code="ai_error_configuration",
            provider_error=original,
        )

        assert exc.code == "ai_error_configuration"
        assert exc.provider_error is original
        assert (
            str(exc)
            == "AssessmentTerminalStageBlockedError(code='ai_error_configuration')"
        )

    def test_provider_error_defaults_to_none(self) -> None:
        exc = AssessmentTerminalStageBlockedError(code="ai_error_configuration")

        assert exc.code == "ai_error_configuration"
        assert exc.provider_error is None

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            AssessmentTerminalStageBlockedError("msg")  # type: ignore[call-arg]


class TestStage4MarkerHierarchy:
    """Stage 4 marker の型階層検証 (foundation marker は production から撤去済)。"""

    def test_recoverable_subclasses_assessment_error(self) -> None:
        assert issubclass(AssessmentRecoverableError, AssessmentError)

    def test_terminal_stage_blocked_subclasses_terminal_error(self) -> None:
        assert issubclass(AssessmentTerminalStageBlockedError, AssessmentTerminalError)

    def test_terminal_target_rejected_subclasses_terminal_error(self) -> None:
        assert issubclass(
            AssessmentTerminalTargetRejectedError, AssessmentTerminalError
        )

    def test_terminal_error_subclasses_assessment_error(self) -> None:
        assert issubclass(AssessmentTerminalError, AssessmentError)

    def test_terminal_error_base_is_abstract(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            AssessmentTerminalError(code="ai_error_configuration")

    def test_terminal_subclass_must_declare_failure_kind(self) -> None:
        with pytest.raises(TypeError, match="FAILURE_KIND"):

            class _MissingFailureKind(AssessmentTerminalError):
                pass

    def test_terminal_markers_subclass_assessment_error(self) -> None:
        assert issubclass(AssessmentTerminalStageBlockedError, AssessmentError)
        assert issubclass(AssessmentTerminalTargetRejectedError, AssessmentError)

    def test_two_markers_are_disjoint(self) -> None:
        # 2 marker の階層は独立 (片方が他方の subclass にならない)。
        assert not issubclass(
            AssessmentRecoverableError, AssessmentTerminalStageBlockedError
        )
        assert not issubclass(
            AssessmentTerminalStageBlockedError, AssessmentRecoverableError
        )
        assert not issubclass(
            AssessmentRecoverableError, AssessmentTerminalTargetRejectedError
        )
        assert not issubclass(
            AssessmentTerminalTargetRejectedError, AssessmentRecoverableError
        )

    def test_assessment_error_is_exception(self) -> None:
        assert issubclass(AssessmentError, Exception)

    def test_marker_classvars_are_audit_projection_ssot(self) -> None:
        assert AssessmentError.STAGE is Stage.ASSESSMENT
        assert AssessmentRecoverableError.FAILURE_KIND == "recoverable"
        assert AssessmentRecoverableError.RETRYABILITY is Retryability.RETRYABLE
        assert AssessmentRecoverableError.FAILURE_ACTION is None
        assert (
            AssessmentTerminalStageBlockedError.FAILURE_KIND == "terminal_stage_blocked"
        )
        assert (
            AssessmentTerminalStageBlockedError.RETRYABILITY
            is Retryability.NON_RETRYABLE
        )
        assert AssessmentTerminalStageBlockedError.FAILURE_ACTION is None
        assert (
            AssessmentTerminalTargetRejectedError.FAILURE_KIND
            == "terminal_target_rejected"
        )
        assert (
            AssessmentTerminalTargetRejectedError.RETRYABILITY
            is Retryability.NON_RETRYABLE
        )
        assert AssessmentTerminalTargetRejectedError.FAILURE_ACTION is None


# Layer 2-B markers (PR2 で追加、Stage 4 工程由来 / provider_error=None 固定)


class _SampleDefect(StrEnum):
    """型ガード契約を検証するためのローカル defect (検知場所の enum を模す)。"""

    SOMETHING = "sample_something"


class TestAssessmentResponseInvalidError:
    """marker ``AssessmentResponseInvalidError`` (Recoverable 系、defect 載せ替え)。

    marker は検知場所の enum を import せず ``StrEnum`` で受ける。code は渡された
    defect の value、型ガードで非 StrEnum を拒否する (PII 境界) ことを isolation で
    確認する (parse / provider の具体 enum とは結合しない)。
    """

    def test_is_recoverable_subclass(self) -> None:
        assert issubclass(AssessmentResponseInvalidError, AssessmentRecoverableError)

    def test_is_assessment_error_subclass(self) -> None:
        assert issubclass(AssessmentResponseInvalidError, AssessmentError)

    def test_code_is_defect_value(self) -> None:
        exc = AssessmentResponseInvalidError(_SampleDefect.SOMETHING)
        assert exc.code == "sample_something"

    def test_provider_error_is_none(self) -> None:
        exc = AssessmentResponseInvalidError(_SampleDefect.SOMETHING)
        assert exc.provider_error is None

    def test_str_renders_code_only(self) -> None:
        exc = AssessmentResponseInvalidError(_SampleDefect.SOMETHING)
        # __str__ は class name + SAFE_ATTRS=("code",) のみ (PII 非露出)
        assert str(exc) == "AssessmentResponseInvalidError(code='sample_something')"

    def test_non_strenum_defect_rejected(self) -> None:
        # 型ガード: 自由文字列 (= PII を載せうる) を ctor に通さない。
        with pytest.raises(TypeError):
            AssessmentResponseInvalidError("schema mismatch")  # type: ignore[arg-type]


class TestAssessmentCategoryMissingError:
    """Layer 2-B marker: ``AssessmentCategoryMissingError`` (non-hold terminal 系)。"""

    def test_is_terminal_error_subclass(self) -> None:
        assert issubclass(AssessmentCategoryMissingError, AssessmentTerminalError)

    def test_is_not_stage_blocked_subclass(self) -> None:
        assert not issubclass(
            AssessmentCategoryMissingError, AssessmentTerminalStageBlockedError
        )

    def test_is_assessment_error_subclass(self) -> None:
        assert issubclass(AssessmentCategoryMissingError, AssessmentError)

    def test_holds_fixed_code(self) -> None:
        exc = AssessmentCategoryMissingError()
        assert exc.code == "assessment_category_missing"

    def test_provider_error_is_none(self) -> None:
        exc = AssessmentCategoryMissingError()
        assert exc.provider_error is None

    def test_failure_kind_is_classification_unresolved(self) -> None:
        assert (
            AssessmentCategoryMissingError.FAILURE_KIND
            == "terminal_classification_unresolved"
        )

    def test_str_renders_code_only(self) -> None:
        exc = AssessmentCategoryMissingError()
        expected = "AssessmentCategoryMissingError(code='assessment_category_missing')"
        assert str(exc) == expected

    def test_positional_message_rejected(self) -> None:
        # Phase 4: 具体 slug は SAFE_ATTRS 外で構造的に SaaS に流れない契約。
        with pytest.raises(TypeError):
            AssessmentCategoryMissingError("unknown slug")  # type: ignore[call-arg]


class TestLayer2BMarkersDisjoint:
    """Layer 2-B 2 marker は互いに独立 (Recoverable と Terminal の階層分離)。"""

    def test_response_invalid_not_terminal(self) -> None:
        assert not issubclass(AssessmentResponseInvalidError, AssessmentTerminalError)

    def test_category_missing_not_recoverable(self) -> None:
        assert not issubclass(
            AssessmentCategoryMissingError, AssessmentRecoverableError
        )
