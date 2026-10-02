"""AIプロバイダー例外の分類・必須の事実・回復の条件の共通契約。"""

from enum import StrEnum

import pytest

from app.ai_providers.errors import (
    CLASSIFIED_AI_PROVIDER_ERRORS,
    AIProviderError,
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderRecovery,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.shared.errors import ApplicationError


class Reason(StrEnum):
    TIMEOUT = "timeout"


def test_base_is_application_error() -> None:
    """プロバイダー例外を共通のアプリケーション例外として扱える。"""
    assert isinstance(AIProviderError(), ApplicationError)


def test_classified_errors_are_the_four_places_where_failure_is_found() -> None:
    """分類済みは、送信前・通信・失敗の応答・生成結果の4つに限る。"""
    assert set(CLASSIFIED_AI_PROVIDER_ERRORS) == {
        AIProviderNotSentError,
        AIProviderTransportError,
        AIProviderResponseError,
        AIProviderResultError,
    }


@pytest.mark.parametrize(
    "error,code",
    [
        (
            AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED),
            "ai_provider_not_sent_error",
        ),
        (
            AIProviderTransportError(
                transport=HttpTransportFailure(
                    HttpTransportStage.CONNECT, HttpTransportFailureReason.TIMEOUT
                )
            ),
            "ai_provider_transport_error",
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.RATE_LIMITED, status_code=429
            ),
            "ai_provider_response_error",
        ),
        (
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_TRUNCATED),
            "ai_provider_result_error",
        ),
    ],
)
def test_classified_error_carries_code_and_reason_in_details(error, code) -> None:
    """ログへ渡す診断には、種類のコードと理由の値だけを入れる。"""
    assert error.CODE == code
    assert error.details == {"code": code, "reason": error.reason.value}


def test_concrete_subclass_is_classified() -> None:
    """分類済みの型を継承した例外も判定対象になる。"""

    class TimeoutFailure(AIProviderTransportError):
        pass

    error = TimeoutFailure(
        transport=HttpTransportFailure(
            HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
        )
    )
    assert isinstance(error, CLASSIFIED_AI_PROVIDER_ERRORS)


def test_bare_base_is_not_classified() -> None:
    """基底型そのものを分類済みとして扱わない。"""
    assert not isinstance(AIProviderError(), CLASSIFIED_AI_PROVIDER_ERRORS)


def test_unknown_direct_subclass_is_not_classified() -> None:
    """CODEを持っていても未知の直接サブクラスは分類済みにしない。"""

    class UnknownFailure(AIProviderError):
        CODE = "unknown_provider_failure"

    assert not isinstance(UnknownFailure(), CLASSIFIED_AI_PROVIDER_ERRORS)


def test_base_keeps_optional_reason_and_message_contract() -> None:
    """基底型は理由を省略でき、説明を省略すると既定の説明を使う。"""
    error = AIProviderError()
    assert error.reason is None
    assert str(error) == "AIプロバイダーの処理に失敗しました"


def test_base_reason_rejects_non_strenum() -> None:
    """StrEnum以外の理由を拒否する。"""
    with pytest.raises(TypeError, match="reason must be a StrEnum member or None"):
        AIProviderError(reason="timeout")  # type: ignore[arg-type]


def test_message_is_preserved() -> None:
    """渡したメッセージを通常のExceptionと同じように保持する。"""
    error = AIProviderResponseError(
        "provider diagnostic message",
        reason=AIProviderResponseReason.SERVER_ERROR,
        status_code=503,
    )
    assert error.args == ("provider diagnostic message",)
    assert str(error) == "provider diagnostic message"


def test_message_rejects_arbitrary_objects() -> None:
    """応答などの任意オブジェクトを説明文として受け取らない。"""
    with pytest.raises(TypeError, match="message must be a string or None"):
        AIProviderResultError(
            {"body": "private-response"},  # type: ignore[arg-type]
            reason=AIProviderResultReason.EMBEDDINGS_EMPTY,
        )


@pytest.mark.parametrize(
    "error,expected_message",
    [
        (
            AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED),
            "AIプロバイダーへのリクエストを送信しませんでした",
        ),
        (
            AIProviderTransportError(
                transport=HttpTransportFailure(
                    HttpTransportStage.SEND, HttpTransportFailureReason.NETWORK_IO
                )
            ),
            "AIプロバイダーとの通信に失敗しました",
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.AUTH, status_code=401
            ),
            "AIプロバイダーが失敗の応答を返しました",
        ),
        (
            AIProviderResultError(reason=AIProviderResultReason.STREAM_INCOMPLETE),
            "AIプロバイダーの生成結果を利用できませんでした",
        ),
    ],
)
def test_no_message_describes_where_failure_was_found(error, expected_message) -> None:
    """説明を省略しても、失敗がどこで判明したかの説明を残す。"""
    assert str(error) == expected_message


def test_request_not_sent_requires_its_own_reason() -> None:
    """送信前の失敗は、送信前の理由だけを受け取る。"""
    with pytest.raises(TypeError, match="reason must be an AIProviderNotSentReason"):
        AIProviderNotSentError(
            reason=AIProviderResultReason.INPUT_BLOCKED  # type: ignore[arg-type]
        )


def test_transport_requires_transport_failure() -> None:
    """通信の失敗は、通信の段階と理由の値を必須で持つ。"""
    with pytest.raises(TypeError, match="transport must be an HttpTransportFailure"):
        AIProviderTransportError(transport="timeout")  # type: ignore[arg-type]


def test_transport_reason_is_the_transport_failure_reason() -> None:
    """通信の失敗は独自の理由を作らず、共通HTTPの理由をそのまま使う。"""
    transport = HttpTransportFailure(
        HttpTransportStage.CONNECT, HttpTransportFailureReason.DNS_RESOLUTION
    )
    error = AIProviderTransportError(transport=transport)
    assert error.transport is transport
    assert error.reason is HttpTransportFailureReason.DNS_RESOLUTION


def test_error_response_requires_its_own_reason() -> None:
    """失敗の応答は、失敗の応答の理由だけを受け取る。"""
    with pytest.raises(TypeError, match="reason must be an AIProviderResponseReason"):
        AIProviderResponseError(
            reason=AIProviderResultReason.INPUT_BLOCKED,  # type: ignore[arg-type]
            status_code=400,
        )


@pytest.mark.parametrize("status_code", ["429", True, None])
def test_error_response_requires_integer_status_code(status_code) -> None:
    """失敗の応答は、HTTP status を整数で必須に持つ。"""
    with pytest.raises(TypeError, match="status_code must be an int"):
        AIProviderResponseError(
            reason=AIProviderResponseReason.RATE_LIMITED,
            status_code=status_code,
        )


def test_error_response_keeps_status_code() -> None:
    """失敗の応答は、受け取った HTTP status を保持する。"""
    error = AIProviderResponseError(
        reason=AIProviderResponseReason.INSUFFICIENT_BALANCE, status_code=402
    )
    assert error.status_code == 402


def test_generation_requires_its_own_reason() -> None:
    """生成結果の失敗は、生成結果の理由だけを受け取る。"""
    with pytest.raises(TypeError, match="reason must be an AIProviderResultReason"):
        AIProviderResultError(
            reason=AIProviderResponseReason.INPUT_BLOCKED  # type: ignore[arg-type]
        )


def test_request_not_sent_needs_operator_action() -> None:
    """送らなかった失敗は、設定や宛先の方針を人が直すまで回復しない。"""
    for reason in AIProviderNotSentReason:
        error = AIProviderNotSentError(reason=reason)
        assert error.recovery is AIProviderRecovery.OPERATOR_ACTION_REQUIRED


@pytest.mark.parametrize(
    "transport,expected",
    [
        (
            HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            ),
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            HttpTransportFailure(
                HttpTransportStage.UNKNOWN, HttpTransportFailureReason.UNKNOWN
            ),
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.PROXY,
                proxy_status=403,
            ),
            AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
        ),
        (
            HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.PROXY,
                proxy_status=502,
            ),
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            HttpTransportFailure(
                HttpTransportStage.CONNECT, HttpTransportFailureReason.PROXY
            ),
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
    ],
)
def test_transport_recovery_is_retry_except_proxy_refusal(transport, expected) -> None:
    """通信の失敗は再試行で治りうるが、proxy の 4xx はこちらの設定による拒否である。"""
    assert AIProviderTransportError(transport=transport).recovery is expected


@pytest.mark.parametrize(
    "reason,expected",
    [
        (
            AIProviderResponseReason.AUTH,
            AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
        ),
        (
            AIProviderResponseReason.LEAKED_API_KEY,
            AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
        ),
        (
            AIProviderResponseReason.PERMISSION_DENIED,
            AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
        ),
        (
            AIProviderResponseReason.NOT_FOUND,
            AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
        ),
        (
            AIProviderResponseReason.FAILED_PRECONDITION,
            AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
        ),
        (
            AIProviderResponseReason.INSUFFICIENT_BALANCE,
            AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
        ),
        (
            AIProviderResponseReason.INVALID_REQUEST,
            AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
        ),
        (
            AIProviderResponseReason.RATE_LIMITED,
            AIProviderRecovery.RECOVERS_AFTER_WAIT,
        ),
        (
            AIProviderResponseReason.QUOTA_EXHAUSTED,
            AIProviderRecovery.RECOVERS_AFTER_WAIT,
        ),
        (
            AIProviderResponseReason.SERVER_ERROR,
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            AIProviderResponseReason.INPUT_TOO_LONG,
            AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT,
        ),
        (
            AIProviderResponseReason.INPUT_BLOCKED,
            AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT,
        ),
    ],
)
def test_error_response_recovery_follows_reason(reason, expected) -> None:
    """失敗の応答の回復の条件は、何が起きたかの理由から決まる。"""
    error = AIProviderResponseError(reason=reason, status_code=400)
    assert error.recovery is expected


def test_every_error_response_reason_has_recovery() -> None:
    """失敗の応答の理由を足したら、回復の条件も決めなければならない。"""
    for reason in AIProviderResponseReason:
        error = AIProviderResponseError(reason=reason, status_code=400)
        assert isinstance(error.recovery, AIProviderRecovery)


@pytest.mark.parametrize(
    "reason,expected",
    [
        (
            AIProviderResultReason.INPUT_BLOCKED,
            AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT,
        ),
        (
            AIProviderResultReason.OUTPUT_BLOCKED_SAFETY,
            AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT,
        ),
        (
            AIProviderResultReason.OUTPUT_BLOCKED_RECITATION,
            AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT,
        ),
        (
            AIProviderResultReason.OUTPUT_BLOCKED_BLOCKLIST,
            AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT,
        ),
        (
            AIProviderResultReason.OUTPUT_BLOCKED_PROHIBITED_CONTENT,
            AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT,
        ),
        (
            AIProviderResultReason.OUTPUT_BLOCKED_SPII,
            AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT,
        ),
        (
            AIProviderResultReason.OUTPUT_TRUNCATED,
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            AIProviderResultReason.STREAM_INCOMPLETE,
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            AIProviderResultReason.EMBEDDINGS_EMPTY,
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            AIProviderResultReason.EMBEDDING_VALUES_MISSING,
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            AIProviderResultReason.EMBEDDING_COUNT_MISMATCH,
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
        (
            AIProviderResultReason.RESPONSE_UNPARSEABLE,
            AIProviderRecovery.MAY_RECOVER_ON_RETRY,
        ),
    ],
)
def test_generation_recovery_follows_reason(reason, expected) -> None:
    """生成結果の失敗の回復の条件は、何が起きたかの理由から決まる。"""
    assert AIProviderResultError(reason=reason).recovery is expected


def test_every_generation_reason_has_recovery() -> None:
    """生成結果の理由を足したら、回復の条件も決めなければならない。"""
    for reason in AIProviderResultReason:
        error = AIProviderResultError(reason=reason)
        assert isinstance(error.recovery, AIProviderRecovery)


def test_unknown_keyword_is_rejected() -> None:
    """未定義のキーワードを黙って捨てない。"""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        AIProviderResultError(
            reason=AIProviderResultReason.OUTPUT_TRUNCATED,
            unrelated_attr="diagnostic",  # type: ignore[call-arg]
        )


def test_cause_chain_is_preserved() -> None:
    """元例外との明示的な原因チェーンを保持する。"""
    cause = TimeoutError("socket timeout")
    with pytest.raises(AIProviderTransportError) as raised:
        raise AIProviderTransportError(
            transport=HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            )
        ) from cause
    assert raised.value.__cause__ is cause


def test_reason_enum_is_preserved_separately_from_args() -> None:
    """基底型の理由は引数とは独立して保持する。"""
    error = AIProviderError("request failed", reason=Reason.TIMEOUT)
    assert error.reason is Reason.TIMEOUT
    assert error.args == ("request failed",)
