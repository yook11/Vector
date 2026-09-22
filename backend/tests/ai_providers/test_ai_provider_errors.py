"""AIプロバイダー例外の継承・引数保持・任意の理由の共通契約。"""

from enum import StrEnum

import pytest

from app.ai_providers.errors import (
    CLASSIFIED_AI_PROVIDER_ERRORS,
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderOutputTruncatedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.shared.errors import ApplicationError


class Reason(StrEnum):
    TIMEOUT = "timeout"


_CONCRETE_ERRORS = (
    (AIProviderInputRejectedError, "ai_error_input_rejected"),
    (AIProviderOutputBlockedError, "ai_error_output_blocked"),
    (AIProviderConfigurationError, "ai_error_configuration"),
    (AIProviderRequestInvalidError, "ai_error_request_invalid"),
    (AIProviderInsufficientBalanceError, "ai_error_insufficient_balance"),
    (AIProviderRateLimitedError, "ai_error_rate_limited"),
    (AIProviderUsageLimitExhaustedError, "ai_error_usage_limit_exhausted"),
    (AIProviderServiceUnavailableError, "ai_error_service_unavailable"),
    (AIProviderNetworkError, "ai_error_network"),
    (AIProviderOutputTruncatedError, "ai_error_output_truncated"),
)
_ERROR_TYPES = (AIProviderError, *(cls for cls, _ in _CONCRETE_ERRORS))


def test_base_is_application_error() -> None:
    """プロバイダー例外を共通のアプリケーション例外として扱える。"""
    assert isinstance(AIProviderError(), ApplicationError)


@pytest.mark.parametrize("cls,code", _CONCRETE_ERRORS)
def test_concrete_error_directly_inherits_base_with_existing_code(cls, code) -> None:
    """既存の具体型とCODEの対応を中間クラスなしで維持する。"""
    assert cls.__bases__ == (AIProviderError,)
    assert cls.CODE == code


def test_classified_errors_cover_existing_concrete_types() -> None:
    """分類済み例外の集合は既存の具体型10種類に限定する。"""
    assert set(CLASSIFIED_AI_PROVIDER_ERRORS) == {cls for cls, _ in _CONCRETE_ERRORS}
    assert len(CLASSIFIED_AI_PROVIDER_ERRORS) == 10


def test_concrete_subclass_is_classified() -> None:
    """具体型を継承した例外も従来どおり判定対象になる。"""

    class NetworkFailure(AIProviderNetworkError):
        pass

    assert isinstance(NetworkFailure(), CLASSIFIED_AI_PROVIDER_ERRORS)


def test_bare_base_is_not_classified() -> None:
    """基底型そのものを分類済みとして扱わない。"""
    assert not isinstance(AIProviderError(), CLASSIFIED_AI_PROVIDER_ERRORS)


def test_unknown_direct_subclass_is_not_classified() -> None:
    """CODEを持っていても未知の直接サブクラスは分類済みにしない。"""

    class UnknownFailure(AIProviderError):
        CODE = "unknown_provider_failure"

    assert not isinstance(UnknownFailure(), CLASSIFIED_AI_PROVIDER_ERRORS)


@pytest.mark.parametrize(
    "cls,expected_message",
    [
        (AIProviderError, "AIプロバイダーの処理に失敗しました"),
        (AIProviderInputRejectedError, "AIプロバイダーが入力を拒否しました"),
        (AIProviderOutputBlockedError, "AIプロバイダーが応答の出力を抑止しました"),
        (AIProviderConfigurationError, "AIプロバイダーの設定または利用条件が不正です"),
        (AIProviderRequestInvalidError, "AIプロバイダーへのリクエストが不正です"),
        (
            AIProviderInsufficientBalanceError,
            "AIプロバイダーの利用残高が不足しています",
        ),
        (AIProviderRateLimitedError, "AIプロバイダーの呼び出し頻度の上限に達しました"),
        (AIProviderUsageLimitExhaustedError, "AIプロバイダーの利用枠を使い切りました"),
        (AIProviderServiceUnavailableError, "AIプロバイダーのサービスを利用できません"),
        (AIProviderNetworkError, "AIプロバイダーとの通信に失敗しました"),
        (
            AIProviderOutputTruncatedError,
            "AIプロバイダーの応答が途中で打ち切られました",
        ),
    ],
)
def test_no_message_describes_known_failure_kind(cls, expected_message) -> None:
    """詳細な理由が不明でも、例外型が表す失敗の説明を残す。"""
    assert str(cls()) == expected_message


@pytest.mark.parametrize("cls", _ERROR_TYPES)
def test_message_is_preserved(cls) -> None:
    """渡したメッセージを通常のExceptionと同じように保持する。"""
    error = cls("provider diagnostic message")
    assert error.args == ("provider diagnostic message",)
    assert str(error) == "provider diagnostic message"


@pytest.mark.parametrize("cls", _ERROR_TYPES)
def test_message_rejects_arbitrary_objects(cls) -> None:
    """応答などの任意オブジェクトを説明文として受け取らない。"""
    with pytest.raises(TypeError, match="message must be a string or None"):
        cls({"body": "private-response"})


@pytest.mark.parametrize("cls", _ERROR_TYPES)
def test_reason_defaults_to_none(cls) -> None:
    """すべての例外で理由を省略できる。"""
    assert cls().reason is None


@pytest.mark.parametrize("cls", _ERROR_TYPES)
def test_reason_accepts_explicit_none(cls) -> None:
    """理由として明示的なNoneを渡せる。"""
    assert cls(reason=None).reason is None


@pytest.mark.parametrize("cls", _ERROR_TYPES)
def test_reason_is_preserved_separately_from_args(cls) -> None:
    """理由は引数とは独立して保持する。"""
    error = cls("request failed", reason=Reason.TIMEOUT)
    assert error.reason is Reason.TIMEOUT
    assert error.args == ("request failed",)
    assert str(error) == "request failed"


@pytest.mark.parametrize("cls", _ERROR_TYPES)
@pytest.mark.parametrize("reason", ["timeout", 1])
def test_reason_rejects_non_strenum(cls, reason) -> None:
    """StrEnum以外の理由を拒否する。"""
    with pytest.raises(TypeError, match="reason must be a StrEnum member or None"):
        cls(reason=reason)


@pytest.mark.parametrize("cls", _ERROR_TYPES)
def test_unknown_keyword_is_rejected(cls) -> None:
    """未定義のキーワードを黙って捨てない。"""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        cls(unrelated_attr="diagnostic")


def test_cause_chain_is_preserved() -> None:
    """元例外との明示的な原因チェーンを保持する。"""
    cause = TimeoutError("socket timeout")
    with pytest.raises(AIProviderNetworkError) as raised:
        raise AIProviderNetworkError("provider timeout") from cause
    assert raised.value.__cause__ is cause
