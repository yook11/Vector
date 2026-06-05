"""``ai_provider_errors`` の 2 系統階層 / 回復クラス (mode) / reason 契約テスト。

検証する不変条件:
- provider error は state / content の 2 系統に分かれ、両者とも ``AIProviderError``
  の subclass。
- 各 leaf は回復クラス ``FAILURE_MODE`` を持つ (state は型で固定、content は
  ``TARGET_REJECTED`` 固定)。state subclass は ``FAILURE_MODE`` 宣言を強制される。
- ``AIProviderFailureMode.retryable`` が回復クラスの retry 可否を表す。
- content の ``reason`` は必須 + StrEnum 型ガード、state の ``reason`` は任意。

``__str__`` / SAFE_ATTRS の PII 境界は ``tests/test_logfire_exceptions.py`` が
正本として所有するため、本ファイルでは重複しない。
"""

from __future__ import annotations

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderContentError,
    AIProviderError,
    AIProviderFailureMode,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderStateError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.gemini_error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)

# state leaf → 回復クラスの golden 写像 (SSoT)。原因 (CODE) ごとの「起きた後の対応」
# を宣言する。値が変わるのは仕様変更時のみ。
_STATE_LEAF_MODE: dict[type[AIProviderStateError], AIProviderFailureMode] = {
    AIProviderNetworkError: AIProviderFailureMode.ATTEMPT_SCOPED,
    AIProviderServiceUnavailableError: AIProviderFailureMode.TIME_BASED_RECOVERY,
    AIProviderRateLimitedError: AIProviderFailureMode.TIME_BASED_RECOVERY,
    AIProviderUsageLimitExhaustedError: AIProviderFailureMode.CONDITION_BASED_RECOVERY,
    AIProviderConfigurationError: AIProviderFailureMode.OPERATOR_ACTION_REQUIRED,
    AIProviderRequestInvalidError: AIProviderFailureMode.OPERATOR_ACTION_REQUIRED,
    AIProviderInsufficientBalanceError: AIProviderFailureMode.OPERATOR_ACTION_REQUIRED,
}

_CONTENT_LEAVES: tuple[type[AIProviderContentError], ...] = (
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)


# -- 回復クラス enum --


def test_failure_mode_has_five_members() -> None:
    """回復クラスは 5 種 (attempt_scoped / time_based / condition_based /
    operator_action / target_rejected)。"""
    assert len(AIProviderFailureMode) == 5


@pytest.mark.parametrize(
    "mode,expected_retryable",
    [
        (AIProviderFailureMode.ATTEMPT_SCOPED, True),
        (AIProviderFailureMode.TIME_BASED_RECOVERY, True),
        (AIProviderFailureMode.CONDITION_BASED_RECOVERY, True),
        (AIProviderFailureMode.OPERATOR_ACTION_REQUIRED, False),
        (AIProviderFailureMode.TARGET_REJECTED, False),
    ],
)
def test_mode_retryable(mode: AIProviderFailureMode, expected_retryable: bool) -> None:
    """retryable は「再試行で回復しうる回復クラスか」を表す。"""
    assert mode.retryable is expected_retryable


# -- 2 系統階層 --


@pytest.mark.parametrize("cls", list(_STATE_LEAF_MODE))
def test_state_leaf_is_state_error_not_content(
    cls: type[AIProviderStateError],
) -> None:
    """state leaf は ``AIProviderStateError`` 配下で ``AIProviderContentError`` 外。"""
    assert issubclass(cls, AIProviderStateError)
    assert issubclass(cls, AIProviderError)
    assert not issubclass(cls, AIProviderContentError)


@pytest.mark.parametrize("cls", _CONTENT_LEAVES)
def test_content_leaf_is_content_error_not_state(
    cls: type[AIProviderContentError],
) -> None:
    """content leaf は ``AIProviderContentError`` 配下、``AIProviderStateError`` 外。"""
    assert issubclass(cls, AIProviderContentError)
    assert issubclass(cls, AIProviderError)
    assert not issubclass(cls, AIProviderStateError)


# -- 回復クラスの leaf への割当 --


@pytest.mark.parametrize("cls,mode", list(_STATE_LEAF_MODE.items()))
def test_state_leaf_failure_mode(
    cls: type[AIProviderStateError], mode: AIProviderFailureMode
) -> None:
    """state leaf の ``FAILURE_MODE`` が golden 写像と一致する。"""
    assert cls.FAILURE_MODE is mode


@pytest.mark.parametrize("cls", _CONTENT_LEAVES)
def test_content_leaf_failure_mode_is_target_rejected(
    cls: type[AIProviderContentError],
) -> None:
    """content leaf の回復クラスは ``TARGET_REJECTED`` 固定。"""
    assert cls.FAILURE_MODE is AIProviderFailureMode.TARGET_REJECTED


def test_state_subclass_without_failure_mode_raises() -> None:
    """``FAILURE_MODE`` を宣言しない state subclass は定義時に ``TypeError``。"""
    with pytest.raises(TypeError, match="FAILURE_MODE"):

        class _BadStateError(AIProviderStateError):
            CODE = "ai_error_bad_for_test"


# -- content reason 契約 (必須 + 型ガード) --


@pytest.mark.parametrize("cls", _CONTENT_LEAVES)
def test_content_reason_is_required(cls: type[AIProviderContentError]) -> None:
    """content error は reason 必須 (検知箇所が拒否理由を必ず上げる)。"""
    with pytest.raises(TypeError):
        cls()  # type: ignore[call-arg]


@pytest.mark.parametrize("cls", _CONTENT_LEAVES)
def test_content_reason_rejects_non_strenum(
    cls: type[AIProviderContentError],
) -> None:
    """content error は自由文字列 reason を ``TypeError`` で拒否する (PII 境界)。"""
    with pytest.raises(TypeError, match="StrEnum"):
        cls(reason="safety")  # type: ignore[arg-type]


@pytest.mark.parametrize("cls", _CONTENT_LEAVES)
def test_content_reason_is_stored(cls: type[AIProviderContentError]) -> None:
    """content error は渡された StrEnum reason を保持する (forensics)。"""
    exc = cls(reason=GeminiContentRejectionReason.RECITATION)
    assert exc.reason is GeminiContentRejectionReason.RECITATION


# -- state reason 契約 (任意 + 型ガード + legacy 互換) --


def test_state_reason_defaults_to_none() -> None:
    """state error の reason は任意 (未指定は None)。"""
    assert AIProviderNetworkError().reason is None


def test_state_reason_accepts_strenum() -> None:
    """state error は StrEnum reason を保持する。"""
    exc = AIProviderServiceUnavailableError(reason=GeminiStateReason.SERVER_ERROR)
    assert exc.reason is GeminiStateReason.SERVER_ERROR


def test_state_reason_rejects_non_strenum() -> None:
    """state error も自由文字列 reason は ``TypeError`` で拒否する (PII 境界)。"""
    with pytest.raises(TypeError, match="StrEnum"):
        AIProviderNetworkError(reason="timeout")  # type: ignore[arg-type]


def test_state_accepts_legacy_positional_message() -> None:
    """state error は legacy positional message を捨てて構築できる (reason は None)。"""
    exc = AIProviderConfigurationError("sensitive sdk message")
    assert exc.reason is None
