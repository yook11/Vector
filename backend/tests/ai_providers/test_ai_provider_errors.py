"""``ai_provider_errors`` の 2 系統階層 / reason 契約テスト。

検証する不変条件:
- provider error は state / content の 2 系統に分かれ、両者とも ``AIProviderError``
  の subclass。
- content の ``reason`` は必須 + StrEnum 型ガード、state の ``reason`` は任意。

``__str__`` / SAFE_ATTRS の PII 境界は ``tests/test_logfire_exceptions.py`` が
正本として所有するため、本ファイルでは重複しない。
"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderContentError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderOutputTruncatedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderStateError,
    AIProviderUsageLimitExhaustedError,
)
from app.ai_providers.gemini.error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)

_STATE_LEAVES: tuple[type[AIProviderStateError], ...] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
    AIProviderOutputTruncatedError,
)

_CONTENT_LEAVES: tuple[type[AIProviderContentError], ...] = (
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)


@pytest.mark.parametrize("cls", _STATE_LEAVES)
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
