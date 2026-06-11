"""``VectorDomainError`` と analysis error class の PII 安全性テスト。

``__str__`` は class name + SAFE_ATTRS の固定形式だけを返す。SDK 生 message や
payload 値が constructor 経由で渡っても、Logfire に載る文字列へ漏れないことを
確認する。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalError,
)
from app.analysis.curation.errors import (
    CurationError,
    CurationRecoverableError,
    CurationResponseInvalidError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
)
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalError,
)
from app.analysis.gemini_error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)
from app.logfire.exceptions import VectorDomainError


class _NoAttrs(VectorDomainError):
    pass


class _WithAttrs(VectorDomainError):
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("alpha", "beta")

    def __init__(self, *, alpha: str, beta: int) -> None:
        super().__init__()
        self.alpha = alpha
        self.beta = beta


def test_str_returns_class_name_only_when_safe_attrs_empty() -> None:
    """``SAFE_ATTRS = ()`` の base path で class name のみ返る (PII 不在の最小形)。"""
    assert str(_NoAttrs()) == "_NoAttrs"


def test_str_formats_safe_attrs_with_repr() -> None:
    """SAFE_ATTRS あり case は ``class(attr=value, ...)`` 固定形式。"""
    out = str(_WithAttrs(alpha="hello", beta=42))
    assert out == "_WithAttrs(alpha='hello', beta=42)"


def test_str_uses_none_for_missing_attribute() -> None:
    """SAFE_ATTRS の一部が instance に無い場合は ``None`` で埋める。"""

    class _PartialAttrs(VectorDomainError):
        SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("present", "absent")

        def __init__(self) -> None:
            super().__init__()
            self.present = "value"

    assert str(_PartialAttrs()) == "_PartialAttrs(present='value', absent=None)"


_AI_PROVIDER_STATE_SUBCLASSES: tuple[tuple[type[AIProviderError], str], ...] = (
    (AIProviderConfigurationError, "ai_error_configuration"),
    (AIProviderRequestInvalidError, "ai_error_request_invalid"),
    (AIProviderInsufficientBalanceError, "ai_error_insufficient_balance"),
    (AIProviderRateLimitedError, "ai_error_rate_limited"),
    (AIProviderUsageLimitExhaustedError, "ai_error_usage_limit_exhausted"),
    (AIProviderServiceUnavailableError, "ai_error_service_unavailable"),
    (AIProviderNetworkError, "ai_error_network"),
)

_AI_PROVIDER_CONTENT_SUBCLASSES: tuple[tuple[type[AIProviderError], str], ...] = (
    (AIProviderInputRejectedError, "ai_error_input_rejected"),
    (AIProviderOutputBlockedError, "ai_error_output_blocked"),
)


@pytest.mark.parametrize("cls,expected_code", _AI_PROVIDER_STATE_SUBCLASSES)
def test_ai_provider_state_error_str_contains_only_code(
    cls: type[AIProviderError], expected_code: str
) -> None:
    """State error の ``__str__`` は class name + CODE のみ (reason は非公開)。"""
    exc = cls()
    assert str(exc) == f"{cls.__name__}(CODE={expected_code!r})"


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_STATE_SUBCLASSES)
def test_ai_provider_state_error_reason_not_in_str(
    cls: type[AIProviderError], _code: str
) -> None:
    """State の reason は forensics 用 instance 属性で ``__str__`` に乗らない。"""
    exc = cls(reason=GeminiStateReason.TIMEOUT)  # type: ignore[call-arg]
    assert "timeout" not in str(exc)
    assert str(exc) == f"{cls.__name__}(CODE={_code!r})"


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_STATE_SUBCLASSES)
def test_ai_provider_state_error_constructor_swallows_legacy_positional_message(
    cls: type[AIProviderError], _code: str
) -> None:
    """legacy positional message は ``__str__`` に漏れない。"""
    sensitive = "sensitive_sdk_message_xxxxxxxxxxxxxxxxxx"
    exc = cls(sensitive)
    rendered = str(exc)
    assert sensitive not in rendered
    assert cls.__name__ in rendered


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_STATE_SUBCLASSES)
def test_ai_provider_state_error_constructor_swallows_legacy_kwargs(
    cls: type[AIProviderError], _code: str
) -> None:
    """legacy kwargs は ``__str__`` に漏れない。"""
    sensitive = "sensitive_kwarg_value_yyyyyyyyyyyyyyyyy"
    exc = cls(unrelated_attr=sensitive)
    rendered = str(exc)
    assert sensitive not in rendered


@pytest.mark.parametrize("cls,expected_code", _AI_PROVIDER_CONTENT_SUBCLASSES)
def test_ai_provider_content_error_str_contains_code_and_reason(
    cls: type[AIProviderError], expected_code: str
) -> None:
    """Content error の ``__str__`` は CODE + reason (PII-free な種別ラベル)。"""
    exc = cls(reason=GeminiContentRejectionReason.SAFETY)  # type: ignore[call-arg]
    rendered = str(exc)
    assert rendered == (
        f"{cls.__name__}(CODE={expected_code!r}, "
        f"reason={GeminiContentRejectionReason.SAFETY!r})"
    )


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_CONTENT_SUBCLASSES)
def test_ai_provider_content_error_requires_reason_kwarg(
    cls: type[AIProviderError], _code: str
) -> None:
    """Content error は reason 必須 (検知箇所が拒否理由を必ず上げる契約)。"""
    with pytest.raises(TypeError):
        cls()  # type: ignore[call-arg]


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_CONTENT_SUBCLASSES)
def test_ai_provider_content_error_rejects_positional_message(
    cls: type[AIProviderError], _code: str
) -> None:
    """Content error は positional message を ``TypeError`` で拒否する (PII 境界)。"""
    with pytest.raises(TypeError):
        cls("sensitive_sdk_message")  # type: ignore[call-arg]


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_CONTENT_SUBCLASSES)
def test_ai_provider_content_error_rejects_non_strenum_reason(
    cls: type[AIProviderError], _code: str
) -> None:
    """Content error は自由文字列 reason を ``TypeError`` で拒否する (PII 境界)。"""
    with pytest.raises(TypeError):
        cls(reason="sensitive_free_text")  # type: ignore[call-arg]


def test_ai_provider_error_base_inherits_vector_domain_error() -> None:
    """``AIProviderError`` は ``VectorDomainError`` 継承 (型階層上の中心契約)。"""
    assert issubclass(AIProviderError, VectorDomainError)


_CURATION_LAYER1_MARKERS: tuple[type[CurationError], ...] = (
    CurationRecoverableError,
    CurationTerminalKeepError,
    CurationTerminalDropError,
)


@pytest.mark.parametrize("cls", _CURATION_LAYER1_MARKERS)
def test_curation_layer1_str_contains_only_code(cls: type[CurationError]) -> None:
    """Curation Layer 1 marker の ``__str__`` は class name + code のみ。"""
    exc = cls(code="ai_error_rate_limited", failure_kind="time_based_recovery")  # type: ignore[call-arg]
    assert str(exc) == f"{cls.__name__}(code='ai_error_rate_limited')"


@pytest.mark.parametrize("cls", _CURATION_LAYER1_MARKERS)
def test_curation_layer1_holds_provider_error_in_attr(
    cls: type[CurationError],
) -> None:
    """``provider_error`` は forensics 用 attr で、``__str__`` には出ない。"""
    provider = AIProviderRateLimitedError()
    exc = cls(  # type: ignore[call-arg]
        code="ai_error_rate_limited",
        failure_kind="time_based_recovery",
        provider_error=provider,
    )
    assert exc.provider_error is provider
    assert "Provider" not in str(exc)
    assert "provider_error" not in str(exc)


@pytest.mark.parametrize("cls", _CURATION_LAYER1_MARKERS)
def test_curation_layer1_rejects_positional_message(
    cls: type[CurationError],
) -> None:
    """Curation Layer 1 marker は positional message を拒否する (PII 境界)。"""
    with pytest.raises(TypeError):
        cls("legacy_message")  # type: ignore[call-arg]


@pytest.mark.parametrize("cls", _CURATION_LAYER1_MARKERS)
def test_curation_layer1_requires_code_kwarg(cls: type[CurationError]) -> None:
    """``code`` は required kwarg (audit 軸が必ず立つことを構造的に保証)。"""
    with pytest.raises(TypeError):
        cls()  # type: ignore[call-arg]


_OTHER_LAYER1_MARKERS: tuple[type[VectorDomainError], ...] = (
    AssessmentRecoverableError,
    AssessmentTerminalError,
    EmbeddingRecoverableError,
    EmbeddingTerminalError,
)


@pytest.mark.parametrize("cls", _OTHER_LAYER1_MARKERS)
def test_assessment_embedding_layer1_str_format(
    cls: type[VectorDomainError],
) -> None:
    """Assessment / Embedding Layer 1 marker の ``__str__`` も code のみ。"""
    exc = cls(code="ai_error_network", failure_kind="attempt_scoped")  # type: ignore[call-arg]
    assert str(exc) == f"{cls.__name__}(code='ai_error_network')"


@pytest.mark.parametrize("cls", _OTHER_LAYER1_MARKERS)
def test_assessment_embedding_layer1_rejects_positional_message(
    cls: type[VectorDomainError],
) -> None:
    with pytest.raises(TypeError):
        cls("legacy_message")  # type: ignore[call-arg]


_LAYER2B_FIXED_CODE: tuple[tuple[type[VectorDomainError], str], ...] = (
    (CurationResponseInvalidError, "extraction_response_invalid"),
    (EmbeddingResponseInvalidError, "embedding_response_invalid"),
)


@pytest.mark.parametrize("cls,fixed_code", _LAYER2B_FIXED_CODE)
def test_layer2b_no_arg_constructor_sets_fixed_code(
    cls: type[VectorDomainError], fixed_code: str
) -> None:
    """Layer 2-B は引数なし construction で固定 code が立つ。"""
    exc = cls()  # type: ignore[call-arg]
    assert getattr(exc, "code", None) == fixed_code
    assert str(exc) == f"{cls.__name__}(code={fixed_code!r})"


@pytest.mark.parametrize("cls,_code", _LAYER2B_FIXED_CODE)
def test_layer2b_rejects_positional_message(
    cls: type[VectorDomainError], _code: str
) -> None:
    """Layer 2-B marker は positional message を拒否する (PII 境界)。"""
    with pytest.raises(TypeError):
        cls("legacy_message")  # type: ignore[call-arg]


def _build_all_marker_instances() -> list[VectorDomainError]:
    """PII 安全性を確認する全 marker class の代表 instance を 1 つずつ構築する。"""
    provider = AIProviderRateLimitedError()
    return [
        AIProviderInputRejectedError(reason=GeminiContentRejectionReason.INPUT_BLOCKED),
        AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
        AIProviderConfigurationError(),
        AIProviderRequestInvalidError(),
        AIProviderInsufficientBalanceError(),
        AIProviderRateLimitedError(),
        AIProviderUsageLimitExhaustedError(),
        AIProviderServiceUnavailableError(),
        AIProviderNetworkError(),
        CurationRecoverableError(
            code="ai_error_rate_limited",
            failure_kind="time_based_recovery",
            provider_error=provider,
        ),
        CurationTerminalKeepError(
            code="ai_error_configuration",
            failure_kind="operator_action_required",
            provider_error=provider,
        ),
        CurationTerminalDropError(
            code="ai_error_input_rejected",
            failure_kind="target_rejected",
            failure_reason="safety",
            provider_error=provider,
        ),
        CurationResponseInvalidError(),
        AssessmentRecoverableError(
            code="ai_error_network",
            failure_kind="attempt_scoped",
            provider_error=provider,
        ),
        AssessmentTerminalError(
            code="ai_error_input_rejected",
            failure_kind="target_rejected",
            failure_reason="safety",
            provider_error=provider,
        ),
        AssessmentResponseInvalidError(AssessmentResponseDefect.CATEGORY_KEY_MISSING),
        EmbeddingRecoverableError(
            code="ai_error_network",
            failure_kind="attempt_scoped",
            provider_error=provider,
        ),
        EmbeddingTerminalError(
            code="ai_error_input_rejected",
            failure_kind="target_rejected",
            failure_reason="safety",
            provider_error=provider,
        ),
        EmbeddingResponseInvalidError(),
    ]


def test_all_marker_str_never_contains_provider_error_repr_payload() -> None:
    """全 marker の ``str(exc)`` に provider_error 由来の文字列が残らない。"""
    instances = _build_all_marker_instances()
    rendered_all = json.dumps(
        [str(exc) for exc in instances],
        default=str,
        ensure_ascii=False,
    )
    assessment_record = str(
        AssessmentRecoverableError(
            code="ai_error_network",
            failure_kind="attempt_scoped",
            provider_error=AIProviderRateLimitedError(),
        )
    )
    assert "rate_limited" not in assessment_record
    assert "AIProviderRateLimitedError" not in assessment_record
    assert all(type(exc).__name__ in rendered_all for exc in instances)


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_STATE_SUBCLASSES)
def test_ai_provider_state_error_full_text_search_no_sensitive(
    cls: type[AIProviderError], _code: str
) -> None:
    """State error に SDK の sensitive message を渡しても全文検索でヒットしない。"""
    sensitive = "sensitive_provider_response_zzzzzzzz"
    exc = cls(sensitive, extra=sensitive)
    dumped = json.dumps(
        {"str": str(exc), "args": getattr(exc, "args", ())},
        default=str,
        ensure_ascii=False,
    )
    assert sensitive not in dumped


def test_stage_base_classes_inherit_vector_domain_error() -> None:
    """各 Stage の base class が ``VectorDomainError`` の subclass であること。"""
    assert issubclass(CurationError, VectorDomainError)
    assert issubclass(AssessmentError, VectorDomainError)
    assert issubclass(EmbeddingError, VectorDomainError)


def test_layer2b_subclasses_inherit_from_layer1_marker() -> None:
    """Layer 2-B class は対応する Layer 1 marker を継承する。"""
    assert issubclass(CurationResponseInvalidError, CurationRecoverableError)
    assert issubclass(AssessmentResponseInvalidError, AssessmentRecoverableError)
    assert issubclass(EmbeddingResponseInvalidError, EmbeddingRecoverableError)


def test_curation_error_is_not_layer1_marker() -> None:
    """``CurationError`` 自体は Layer 1 marker の subclass ではない。"""
    sample: Any = CurationError()
    assert not isinstance(sample, CurationRecoverableError)
    assert not isinstance(sample, CurationTerminalKeepError)
    assert not isinstance(sample, CurationTerminalDropError)
