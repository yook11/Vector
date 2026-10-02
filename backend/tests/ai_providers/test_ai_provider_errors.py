"""AIプロバイダー例外の分類と必須の事実の共通契約。"""

from datetime import UTC, datetime

import pytest

from app.ai_providers.errors import (
    CLASSIFIED_AI_PROVIDER_ERRORS,
    AIProviderError,
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.shared.errors import ApplicationError

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def test_base_is_application_error() -> None:
    """プロバイダー例外を共通のアプリケーション例外として扱える。"""
    assert issubclass(AIProviderError, ApplicationError)


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
                http_error=HttpTransportError(
                    failure=HttpTransportFailure(
                        HttpTransportStage.CONNECT, HttpTransportFailureReason.TIMEOUT
                    )
                )
            ),
            "ai_provider_transport_error",
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.RATE_LIMITED,
                http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
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
        http_error=HttpTransportError(
            failure=HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            )
        )
    )
    assert isinstance(error, CLASSIFIED_AI_PROVIDER_ERRORS)


def test_base_cannot_be_instantiated_directly() -> None:
    """どこで判明したかを持たない基底型のままでは作れない。"""
    with pytest.raises(
        TypeError, match="AIProviderError cannot be instantiated directly"
    ):
        AIProviderError(reason=AIProviderResultReason.RESPONSE_UNPARSEABLE)


def test_unknown_direct_subclass_is_not_classified() -> None:
    """CODEを持っていても未知の直接サブクラスは分類済みにしない。"""

    class UnknownFailure(AIProviderError):
        CODE = "unknown_provider_failure"

    error = UnknownFailure(reason=AIProviderResultReason.RESPONSE_UNPARSEABLE)
    assert not isinstance(error, CLASSIFIED_AI_PROVIDER_ERRORS)


def test_reason_rejects_non_strenum() -> None:
    """StrEnum以外の理由を拒否する。"""

    class UnknownFailure(AIProviderError):
        pass

    with pytest.raises(TypeError, match="reason must be a StrEnum member"):
        UnknownFailure(reason="timeout")  # type: ignore[arg-type]


def test_message_is_preserved() -> None:
    """渡したメッセージを通常のExceptionと同じように保持する。"""
    error = AIProviderResponseError(
        "provider diagnostic message",
        reason=AIProviderResponseReason.SERVER_ERROR,
        http_error=HttpResponseError(status_code=503, received_at=_RECEIVED_AT),
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
                http_error=HttpTransportError(
                    failure=HttpTransportFailure(
                        HttpTransportStage.SEND, HttpTransportFailureReason.NETWORK_IO
                    )
                )
            ),
            "AIプロバイダーとの通信に失敗しました",
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.AUTH,
                http_error=HttpResponseError(status_code=401, received_at=_RECEIVED_AT),
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


def test_not_sent_requires_its_own_reason() -> None:
    """送信前の失敗は、送信前の理由だけを受け取る。"""
    with pytest.raises(TypeError, match="reason must be an AIProviderNotSentReason"):
        AIProviderNotSentError(
            reason=AIProviderResultReason.INPUT_BLOCKED  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "http_error",
    [
        HttpTransportFailure(
            HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
        ),
        HttpResponseError(status_code=503, received_at=_RECEIVED_AT),
    ],
)
def test_transport_requires_http_transport_error(http_error) -> None:
    """通信の失敗は、HTTPの通信失敗のエラーを必須で持つ。"""
    with pytest.raises(TypeError, match="http_error must be an HttpTransportError"):
        AIProviderTransportError(http_error=http_error)


def test_transport_keeps_http_error_and_uses_its_reason() -> None:
    """通信の失敗はHTTPのエラーをそのまま持ち、独自の理由を作らない。"""
    http_error = HttpTransportError(
        failure=HttpTransportFailure(
            HttpTransportStage.CONNECT, HttpTransportFailureReason.DNS_RESOLUTION
        )
    )
    error = AIProviderTransportError(http_error=http_error)
    assert error.http_error is http_error
    assert error.reason is HttpTransportFailureReason.DNS_RESOLUTION


def test_response_requires_its_own_reason() -> None:
    """失敗の応答は、失敗の応答の理由だけを受け取る。"""
    with pytest.raises(TypeError, match="reason must be an AIProviderResponseReason"):
        AIProviderResponseError(
            reason=AIProviderResultReason.INPUT_BLOCKED,  # type: ignore[arg-type]
            http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
        )


@pytest.mark.parametrize(
    "http_error",
    [
        429,
        HttpTransportError(
            failure=HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            )
        ),
    ],
)
def test_response_requires_http_response_error(http_error) -> None:
    """失敗の応答は、HTTPの非成功応答のエラーを必須で持つ。"""
    with pytest.raises(TypeError, match="http_error must be an HttpResponseError"):
        AIProviderResponseError(
            reason=AIProviderResponseReason.RATE_LIMITED,
            http_error=http_error,
        )


def test_response_keeps_http_error() -> None:
    """失敗の応答は、受け取った応答の事実をHTTPのエラーのまま保持する。"""
    http_error = HttpResponseError(
        status_code=429, received_at=_RECEIVED_AT, retry_after="30"
    )
    error = AIProviderResponseError(
        reason=AIProviderResponseReason.RATE_LIMITED, http_error=http_error
    )
    assert error.http_error is http_error


def test_result_requires_its_own_reason() -> None:
    """生成結果の失敗は、生成結果の理由だけを受け取る。"""
    with pytest.raises(TypeError, match="reason must be an AIProviderResultReason"):
        AIProviderResultError(
            reason=AIProviderResponseReason.INPUT_BLOCKED  # type: ignore[arg-type]
        )


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
            http_error=HttpTransportError(
                failure=HttpTransportFailure(
                    HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                )
            )
        ) from cause
    assert raised.value.__cause__ is cause


def test_reason_enum_is_preserved_separately_from_args() -> None:
    """理由は引数とは独立して保持する。"""
    error = AIProviderResultError(
        "request failed", reason=AIProviderResultReason.RESPONSE_UNPARSEABLE
    )
    assert error.reason is AIProviderResultReason.RESPONSE_UNPARSEABLE
    assert error.args == ("request failed",)
