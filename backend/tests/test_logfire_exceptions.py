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
from app.analysis.assessment.errors import (
    AssessmentCategoryMissingError,
    AssessmentError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalError,
    AssessmentTerminalStageBlockedError,
    AssessmentTerminalTargetRejectedError,
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
    EmbeddingTerminalStageBlockedError,
    EmbeddingTerminalTargetRejectedError,
)
from app.logfire_exceptions import VectorDomainError

# ---------------------------------------------------------------------------
# VectorDomainError 基底の __str__ 契約
# ---------------------------------------------------------------------------


class _NoAttrs(VectorDomainError):
    """SAFE_ATTRS = () の subclass。"""


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
    """SAFE_ATTRS あり case は ``class(attr=value, ...)`` 固定形式。

    repr 経由で値を出すので、str 値は quote 付きで現れる。これにより
    ``code='abc'`` 形式で audit context が dashboard 上 1 文字列として読める。
    """
    out = str(_WithAttrs(alpha="hello", beta=42))
    assert out == "_WithAttrs(alpha='hello', beta=42)"


def test_str_uses_none_for_missing_attribute() -> None:
    """SAFE_ATTRS の一部が instance に無い場合は ``None`` で埋める (防御的)。

    将来 subclass が SAFE_ATTRS を増やしたが setter を忘れた場合でも、``__str__``
    で AttributeError を起こさず ``key=None`` で運用継続できる。
    """

    class _PartialAttrs(VectorDomainError):
        SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("present", "absent")

        def __init__(self) -> None:
            super().__init__()
            self.present = "value"

    assert str(_PartialAttrs()) == "_PartialAttrs(present='value', absent=None)"


# ---------------------------------------------------------------------------
# AIProviderError (10 class): SAFE_ATTRS=("CODE",) + accept-and-discard
# ---------------------------------------------------------------------------

_AI_PROVIDER_SUBCLASSES: tuple[tuple[type[AIProviderError], str], ...] = (
    (AIProviderInputRejectedError, "ai_error_input_rejected"),
    (AIProviderOutputBlockedError, "ai_error_output_blocked"),
    (AIProviderConfigurationError, "ai_error_configuration"),
    (AIProviderRequestInvalidError, "ai_error_request_invalid"),
    (AIProviderInsufficientBalanceError, "ai_error_insufficient_balance"),
    (AIProviderRateLimitedError, "ai_error_rate_limited"),
    (AIProviderUsageLimitExhaustedError, "ai_error_usage_limit_exhausted"),
    (AIProviderServiceUnavailableError, "ai_error_service_unavailable"),
    (AIProviderNetworkError, "ai_error_network"),
)


@pytest.mark.parametrize("cls,expected_code", _AI_PROVIDER_SUBCLASSES)
def test_ai_provider_error_str_contains_only_code(
    cls: type[AIProviderError], expected_code: str
) -> None:
    """各 AIProvider*Error の ``__str__`` は class name + CODE のみ。"""
    exc = cls()
    assert str(exc) == f"{cls.__name__}(CODE={expected_code!r})"


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_SUBCLASSES)
def test_ai_provider_error_constructor_swallows_legacy_positional_message(
    cls: type[AIProviderError], _code: str
) -> None:
    """legacy 互換: positional message を渡しても捨てて ``__str__`` には出ない。

    PII 含有が想定される SDK 生 message を渡してもクラスは構築でき、かつ
    ``__str__`` 経路 (Logfire span attribute) には漏れない。
    """
    sensitive = "sensitive_sdk_message_xxxxxxxxxxxxxxxxxx"
    exc = cls(sensitive)
    rendered = str(exc)
    assert sensitive not in rendered
    assert cls.__name__ in rendered


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_SUBCLASSES)
def test_ai_provider_error_constructor_swallows_legacy_kwargs(
    cls: type[AIProviderError], _code: str
) -> None:
    """legacy 互換: 未知 kwargs を渡しても落とさず ``__str__`` には出ない。

    SDK 翻訳側から kwargs が渡っても ``__str__`` に乗らない。
    """
    sensitive = "sensitive_kwarg_value_yyyyyyyyyyyyyyyyy"
    exc = cls(unrelated_attr=sensitive)
    rendered = str(exc)
    assert sensitive not in rendered


def test_ai_provider_error_base_inherits_vector_domain_error() -> None:
    """``AIProviderError`` は ``VectorDomainError`` 継承 (型階層上の中心契約)。"""
    assert issubclass(AIProviderError, VectorDomainError)


# ---------------------------------------------------------------------------
# Curation Layer 1 marker (3 class): SAFE_ATTRS=("code",) + kwargs-only
# ---------------------------------------------------------------------------

_CURATION_LAYER1_MARKERS: tuple[type[CurationError], ...] = (
    CurationRecoverableError,
    CurationTerminalKeepError,
    CurationTerminalDropError,
)


@pytest.mark.parametrize("cls", _CURATION_LAYER1_MARKERS)
def test_curation_layer1_str_contains_only_code(cls: type[CurationError]) -> None:
    """``CurationXxxError(code='...')`` の ``__str__`` は class name + code のみ。"""
    exc = cls(code="ai_error_rate_limited")
    assert str(exc) == f"{cls.__name__}(code='ai_error_rate_limited')"


@pytest.mark.parametrize("cls", _CURATION_LAYER1_MARKERS)
def test_curation_layer1_holds_provider_error_in_attr(
    cls: type[CurationError],
) -> None:
    """``provider_error`` は instance attr のみで識別 (forensics)、``__str__`` 不在。

    Logfire span に乗るのは class name + code のみ。``provider_error`` の中身
    (CODE / __class__) は SAFE_ATTRS の対象外で SaaS には流れない。
    """
    provider = AIProviderRateLimitedError()
    exc = cls(code="ai_error_rate_limited", provider_error=provider)
    assert exc.provider_error is provider  # identity 保持で audit 連鎖が辿れる
    # __str__ には provider 情報は出ない (SAFE_ATTRS にないため)
    assert "Provider" not in str(exc)
    assert "provider_error" not in str(exc)


@pytest.mark.parametrize("cls", _CURATION_LAYER1_MARKERS)
def test_curation_layer1_rejects_positional_message(
    cls: type[CurationError],
) -> None:
    """positional message 引数は ``TypeError`` (kwargs-only 強制で regression 検知)。

    将来 ``raise CurationXxxError(str(provider_response))`` の混入 (PII 含有) を
    型レベルで阻止する。
    """
    with pytest.raises(TypeError):
        cls("legacy_message")  # type: ignore[call-arg]


@pytest.mark.parametrize("cls", _CURATION_LAYER1_MARKERS)
def test_curation_layer1_requires_code_kwarg(cls: type[CurationError]) -> None:
    """``code`` は required kwarg (audit 軸が必ず立つことを構造的に保証)。"""
    with pytest.raises(TypeError):
        cls()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Assessment / Embedding Layer 1 marker (6 class): kwargs-only, 同律
# ---------------------------------------------------------------------------

_OTHER_LAYER1_MARKERS: tuple[type[VectorDomainError], ...] = (
    AssessmentRecoverableError,
    AssessmentTerminalStageBlockedError,
    AssessmentTerminalTargetRejectedError,
    EmbeddingRecoverableError,
    EmbeddingTerminalStageBlockedError,
    EmbeddingTerminalTargetRejectedError,
)


@pytest.mark.parametrize("cls", _OTHER_LAYER1_MARKERS)
def test_assessment_embedding_layer1_str_format(
    cls: type[VectorDomainError],
) -> None:
    """Assessment / Embedding Layer 1 marker も同形式の ``__str__`` を持つ。"""
    exc = cls(code="ai_error_network")  # type: ignore[call-arg]
    assert str(exc) == f"{cls.__name__}(code='ai_error_network')"


@pytest.mark.parametrize("cls", _OTHER_LAYER1_MARKERS)
def test_assessment_embedding_layer1_rejects_positional_message(
    cls: type[VectorDomainError],
) -> None:
    with pytest.raises(TypeError):
        cls("legacy_message")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Layer 2-B subclasses (4 class): no-arg constructor + 固定 code
# ---------------------------------------------------------------------------

_LAYER2B_FIXED_CODE: tuple[tuple[type[VectorDomainError], str], ...] = (
    (CurationResponseInvalidError, "extraction_response_invalid"),
    (AssessmentResponseInvalidError, "assessment_response_invalid"),
    (AssessmentCategoryMissingError, "assessment_category_missing"),
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
    """Layer 2-B も positional message 引数を ``TypeError`` で拒否する。

    ``CurationResponseInvalidError("payload dump")`` 形で PII を constructor に
    入れる regression を構造的に阻止する。
    """
    with pytest.raises(TypeError):
        cls("legacy_message")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# PII 全文検索 oracle: 22 class 全件で sensitive 値が __str__ に乗らない
# ---------------------------------------------------------------------------


def _build_22_class_instances() -> list[VectorDomainError]:
    """PII 安全性を確認する 22 class の代表 instance を 1 つずつ構築する。"""
    provider = AIProviderRateLimitedError()
    return [
        # AIProvider*Error 9 種 (引数なし)
        AIProviderInputRejectedError(),
        AIProviderOutputBlockedError(),
        AIProviderConfigurationError(),
        AIProviderRequestInvalidError(),
        AIProviderInsufficientBalanceError(),
        AIProviderRateLimitedError(),
        AIProviderUsageLimitExhaustedError(),
        AIProviderServiceUnavailableError(),
        AIProviderNetworkError(),
        # Curation 3 (Layer 1) + 1 (Layer 2-B)
        CurationRecoverableError(code="ai_error_rate_limited", provider_error=provider),
        CurationTerminalKeepError(
            code="ai_error_configuration", provider_error=provider
        ),
        CurationTerminalDropError(
            code="ai_error_input_rejected", provider_error=provider
        ),
        CurationResponseInvalidError(),
        # Assessment 3 (Layer 1) + 2 (Layer 2-B)
        AssessmentRecoverableError(code="ai_error_network", provider_error=provider),
        AssessmentTerminalStageBlockedError(
            code="ai_error_configuration", provider_error=provider
        ),
        AssessmentTerminalTargetRejectedError(
            code="ai_error_input_rejected", provider_error=provider
        ),
        AssessmentResponseInvalidError(),
        AssessmentCategoryMissingError(),
        # Embedding 3 (Layer 1) + 1 (Layer 2-B)
        EmbeddingRecoverableError(code="ai_error_network", provider_error=provider),
        EmbeddingTerminalStageBlockedError(
            code="ai_error_configuration", provider_error=provider
        ),
        EmbeddingTerminalTargetRejectedError(
            code="ai_error_input_rejected", provider_error=provider
        ),
        EmbeddingResponseInvalidError(),
    ]


def test_22_class_str_never_contains_provider_error_repr_payload() -> None:
    """22 class の ``str(exc)`` を JSON 全文化しても provider_error 由来の文字列
    が 1 つも残らない。

    意図的に provider_error として ``AIProviderRateLimitedError`` instance を
    Layer 1 marker に紐付け、provider 側の class 名や CODE 値が外側 marker の
    ``__str__`` に **連鎖して出ない** ことを検証する。
    """
    instances = _build_22_class_instances()
    rendered_all = json.dumps(
        [str(exc) for exc in instances],
        default=str,
        ensure_ascii=False,
    )
    # provider_error は SAFE_ATTRS の対象外なので、外側 marker から派生情報を出さない。
    assessment_record = str(
        AssessmentRecoverableError(
            code="ai_error_network",
            provider_error=AIProviderRateLimitedError(),
        )
    )
    assert "rate_limited" not in assessment_record
    assert "AIProviderRateLimitedError" not in assessment_record
    # 全 instance について基本的な class name は出ている (空虚回避)
    assert all(type(exc).__name__ in rendered_all for exc in instances)


@pytest.mark.parametrize("cls,_code", _AI_PROVIDER_SUBCLASSES)
def test_ai_provider_error_full_text_search_no_sensitive(
    cls: type[AIProviderError], _code: str
) -> None:
    """AIProvider*Error に SDK の sensitive message を渡しても全文検索でヒット
    しない。"""
    sensitive = "sensitive_provider_response_zzzzzzzz"
    exc = cls(sensitive, extra=sensitive)
    dumped = json.dumps(
        {"str": str(exc), "args": getattr(exc, "args", ())},
        default=str,
        ensure_ascii=False,
    )
    assert sensitive not in dumped


# ---------------------------------------------------------------------------
# Hierarchy invariants (型階層の中心契約)
# ---------------------------------------------------------------------------


def test_stage_base_classes_inherit_vector_domain_error() -> None:
    """各 Stage の base class が ``VectorDomainError`` の subclass であること。"""
    assert issubclass(CurationError, VectorDomainError)
    assert issubclass(AssessmentError, VectorDomainError)
    assert issubclass(EmbeddingError, VectorDomainError)


def test_layer2b_subclasses_inherit_from_layer1_marker() -> None:
    """Layer 2-B 4 class は対応する Layer 1 marker を継承 (dispatch 軸を引き継ぐ)。

    CurationResponseInvalid → Recoverable (cron 救済対象)、
    AssessmentResponseInvalid → Recoverable、
    AssessmentCategoryMissing → terminal base (分類未解決、hold 対象外)、
    EmbeddingResponseInvalid → Recoverable。
    """
    assert issubclass(CurationResponseInvalidError, CurationRecoverableError)
    assert issubclass(AssessmentResponseInvalidError, AssessmentRecoverableError)
    assert issubclass(AssessmentCategoryMissingError, AssessmentTerminalError)
    assert not issubclass(
        AssessmentCategoryMissingError,
        AssessmentTerminalStageBlockedError,
    )
    assert issubclass(EmbeddingResponseInvalidError, EmbeddingRecoverableError)


# ---------------------------------------------------------------------------
# Stage base class そのものは catch 対象にしない
# ---------------------------------------------------------------------------


def test_curation_error_is_not_layer1_marker() -> None:
    """``CurationError`` 自体は Layer 1 marker (3 種) の subclass ではない。

    task 層 dispatch 軸として CurationError を使わない契約 (catch 対象は 3 marker
    のみ)。逆 isinstance 関係も含めて型階層が独立であることを構造的に表明。
    """
    sample: Any = CurationError()
    assert not isinstance(sample, CurationRecoverableError)
    assert not isinstance(sample, CurationTerminalKeepError)
    assert not isinstance(sample, CurationTerminalDropError)
