"""Stage 4 (Assessment) Layer 1 marker の振る舞いテスト。"""

from __future__ import annotations

import pytest

from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.analysis.errors.provider import (
    AIProviderConfigurationError,
    AIProviderRateLimitedError,
)
from app.observability.categories import (
    NonRetryableDropArticle,
    NonRetryableKeepArticle,
    RetryableError,
)


class TestAssessmentRecoverableError:
    """``AssessmentRecoverableError`` の constructor / instance attr 振る舞い。"""

    def test_holds_code_and_provider_error(self) -> None:
        original = AIProviderRateLimitedError("rate limited")
        exc = AssessmentRecoverableError(
            "wrapped",
            code="ai_error_rate_limited",
            provider_error=original,
        )

        assert exc.code == "ai_error_rate_limited"
        assert exc.provider_error is original
        assert str(exc) == "wrapped"

    def test_provider_error_defaults_to_none(self) -> None:
        # PR2 の Layer 2-B で provider_error なしで raise するための準備。
        exc = AssessmentRecoverableError(
            "no provider",
            code="assessment_response_invalid",
        )

        assert exc.code == "assessment_response_invalid"
        assert exc.provider_error is None

    def test_message_defaults_to_empty_string(self) -> None:
        exc = AssessmentRecoverableError(code="x")

        assert exc.code == "x"
        assert exc.provider_error is None
        assert str(exc) == ""

    def test_code_is_keyword_only_required(self) -> None:
        # ``code`` は keyword-only かつ required (positional 渡しは reject)。
        with pytest.raises(TypeError):
            AssessmentRecoverableError("msg")  # type: ignore[call-arg]


class TestAssessmentTerminalSkipError:
    """``AssessmentTerminalSkipError`` の constructor / instance attr 振る舞い。"""

    def test_holds_code_and_provider_error(self) -> None:
        original = AIProviderConfigurationError("bad api key")
        exc = AssessmentTerminalSkipError(
            "wrapped",
            code="ai_error_configuration",
            provider_error=original,
        )

        assert exc.code == "ai_error_configuration"
        assert exc.provider_error is original
        assert str(exc) == "wrapped"

    def test_provider_error_defaults_to_none(self) -> None:
        exc = AssessmentTerminalSkipError(
            "no provider",
            code="assessment_category_missing",
        )

        assert exc.code == "assessment_category_missing"
        assert exc.provider_error is None

    def test_code_is_keyword_only_required(self) -> None:
        with pytest.raises(TypeError):
            AssessmentTerminalSkipError("msg")  # type: ignore[call-arg]


class TestStage4MarkerHierarchy:
    """Stage 4 marker の型階層 / foundation 非継承の検証。"""

    def test_recoverable_subclasses_assessment_error(self) -> None:
        assert issubclass(AssessmentRecoverableError, AssessmentError)

    def test_terminal_skip_subclasses_assessment_error(self) -> None:
        assert issubclass(AssessmentTerminalSkipError, AssessmentError)

    def test_recoverable_does_not_inherit_foundation_markers(self) -> None:
        # 原則 2: Stage 共通 marker は作らない。foundation marker (RetryableError 等)
        # は Stage 3 のものなので、Stage 4 markers は継承しない。
        assert not issubclass(AssessmentRecoverableError, RetryableError)
        assert not issubclass(AssessmentRecoverableError, NonRetryableKeepArticle)
        assert not issubclass(AssessmentRecoverableError, NonRetryableDropArticle)

    def test_terminal_skip_does_not_inherit_foundation_markers(self) -> None:
        assert not issubclass(AssessmentTerminalSkipError, RetryableError)
        assert not issubclass(AssessmentTerminalSkipError, NonRetryableKeepArticle)
        assert not issubclass(AssessmentTerminalSkipError, NonRetryableDropArticle)

    def test_two_markers_are_disjoint(self) -> None:
        # 2 marker の階層は独立 (片方が他方の subclass にならない)。
        assert not issubclass(AssessmentRecoverableError, AssessmentTerminalSkipError)
        assert not issubclass(AssessmentTerminalSkipError, AssessmentRecoverableError)

    def test_assessment_error_is_exception(self) -> None:
        assert issubclass(AssessmentError, Exception)
