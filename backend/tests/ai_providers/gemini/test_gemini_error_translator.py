"""``app.ai_providers.gemini.error_translator`` の golden table テスト。

工程で共有される SDK 例外 → ``AIProvider*Error`` 分類を検証する。
ValidationError / response shape / finish_reason など工程固有の判定は対象外。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from google.genai import errors as genai_errors

from app.ai_providers.errors import (
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderTransportError,
)
from app.ai_providers.gemini.error_translator import translate_gemini_error
from app.http.destination_policy import HostBlockedError
from app.http.destination_resolution import HostResolutionError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)


def _client_error(
    *, code: int, status: str, message: str = "msg"
) -> genai_errors.ClientError:
    """``ClientError(code, response_json)`` を簡易構築する helper。"""
    response_json = {"error": {"status": status, "message": message}}
    return genai_errors.ClientError(code, response_json)


def _server_error(
    *, code: int = 500, status: str = "INTERNAL", message: str = "msg"
) -> genai_errors.ServerError:
    response_json = {"error": {"status": status, "message": message}}
    return genai_errors.ServerError(code, response_json)


def _api_error(
    *, code: int, status: str, message: str = "msg"
) -> genai_errors.APIError:
    """``APIError`` 直接の分類ルート。"""
    response_json = {"error": {"code": code, "status": status, "message": message}}
    return genai_errors.APIError(code, response_json)


# 送信前・通信


def test_host_blocked_is_request_not_sent() -> None:
    """宛先の方針による拒否は、通信の失敗ではなく送らなかった失敗とする。"""
    translated = translate_gemini_error(HostBlockedError("private address"))

    assert isinstance(translated, AIProviderNotSentError)
    assert translated.reason is AIProviderNotSentReason.HOST_BLOCKED


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            httpx.ReadTimeout("timed out"),
            HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            ),
        ),
        (
            httpx.ConnectError("connection refused"),
            HttpTransportFailure(
                HttpTransportStage.CONNECT, HttpTransportFailureReason.NETWORK_IO
            ),
        ),
        (
            httpx.PoolTimeout("pool exhausted"),
            HttpTransportFailure(
                HttpTransportStage.PREPARATION, HttpTransportFailureReason.TIMEOUT
            ),
        ),
        (
            httpx.ProxyError("403 Forbidden"),
            HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.PROXY,
                proxy_status=403,
            ),
        ),
        (
            httpx.ReadError("read failed"),
            HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.NETWORK_IO
            ),
        ),
        (
            httpx.RemoteProtocolError("bad response"),
            HttpTransportFailure(
                HttpTransportStage.RECEIVE,
                HttpTransportFailureReason.PROTOCOL_VIOLATION,
            ),
        ),
        (
            HostResolutionError("no address"),
            HttpTransportFailure(
                HttpTransportStage.PREPARATION,
                HttpTransportFailureReason.DNS_RESOLUTION,
            ),
        ),
    ],
)
def test_common_http_failures_keep_stage_and_reason(
    exc: Exception, expected: HttpTransportFailure
) -> None:
    """共通HTTPの分類結果を、段階と理由ごと通信の失敗に載せる。"""
    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderTransportError)
    assert translated.http_error.failure == expected


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("io timeout"),
        ConnectionError("conn reset"),
        OSError("no such file"),
    ],
)
def test_builtin_errors_are_not_classified(exc: Exception) -> None:
    """HTTP の事実を持たない組み込みの例外は、通信の失敗と決めつけずに返す。"""
    assert translate_gemini_error(exc) is exc


def test_unsupported_protocol_is_not_a_transport_error() -> None:
    """http/https 以外の拒否は通信の失敗ではないので、変換せずに返す。"""
    exc = httpx.UnsupportedProtocol("ftp is not sent")
    assert translate_gemini_error(exc) is exc


# 失敗の応答


def test_server_error_is_error_response_with_status() -> None:
    translated = translate_gemini_error(_server_error(code=503))

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.SERVER_ERROR
    assert translated.http_error.status_code == 503


def test_response_keeps_retry_after_from_sdk_response() -> None:
    """SDK が持つ応答の Retry-After を、解釈せずに HTTP のエラーへ残す。"""
    response = httpx.Response(
        429,
        headers={"Retry-After": "30"},
        request=httpx.Request("POST", "https://generativelanguage.example.invalid"),
    )
    exc = genai_errors.ClientError(
        429,
        {"error": {"status": "RESOURCE_EXHAUSTED", "message": "msg"}},
        response,
    )

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.http_error.retry_after == "30"


def test_response_without_sdk_response_has_no_retry_after() -> None:
    """SDK が応答を持たなければ、Retry-After も無いとする。"""
    translated = translate_gemini_error(
        _client_error(code=429, status="RESOURCE_EXHAUSTED")
    )

    assert isinstance(translated, AIProviderResponseError)
    assert translated.http_error.retry_after is None


def test_response_received_at_is_when_the_error_was_translated() -> None:
    """受信時刻は、SDK の例外を捕まえて変換した時刻とする。"""
    before = datetime.now(UTC)
    translated = translate_gemini_error(_server_error(code=503))
    after = datetime.now(UTC)

    assert isinstance(translated, AIProviderResponseError)
    assert before <= translated.http_error.received_at <= after


def test_translator_does_not_copy_leaked_key_message() -> None:
    """変換時にSDKの漏洩キー情報を例外メッセージへ取り込まない。"""
    sdk_message = (
        "API key AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q has been "
        "reported as leaked at https://github.com/foo/bar"
    )
    exc = _client_error(code=400, status="INVALID_ARGUMENT", message=sdk_message)

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.LEAKED_API_KEY
    assert "AIza" not in str(translated)
    assert "github.com" not in str(translated)
    assert str(translated) == "AIプロバイダーがAPIキーの漏洩を検知しました"


@pytest.mark.parametrize(
    "code,status,expected",
    [
        (401, "UNAUTHENTICATED", AIProviderResponseReason.AUTH),
        (403, "PERMISSION_DENIED", AIProviderResponseReason.PERMISSION_DENIED),
        (404, "NOT_FOUND", AIProviderResponseReason.NOT_FOUND),
        (
            400,
            "FAILED_PRECONDITION",
            AIProviderResponseReason.FAILED_PRECONDITION,
        ),
    ],
)
def test_config_status_is_error_response_with_reason(
    code: int, status: str, expected: AIProviderResponseReason
) -> None:
    exc = _client_error(code=code, status=status, message="config issue")

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is expected
    assert translated.http_error.status_code == code


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, AIProviderResponseReason.AUTH),
        (403, AIProviderResponseReason.PERMISSION_DENIED),
        (404, AIProviderResponseReason.NOT_FOUND),
    ],
)
def test_config_http_code_without_status(
    code: int, expected: AIProviderResponseReason
) -> None:
    """status が空でも HTTP code (401/403/404) で設定系の理由に振る。"""
    exc = _client_error(code=code, status="", message="config")

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is expected


def test_failed_precondition_with_code_400_evaluates_status_first() -> None:
    """``code=400`` と ``status=FAILED_PRECONDITION`` が同居しても status 優先。"""
    exc = _client_error(
        code=400,
        status="FAILED_PRECONDITION",
        message="model not enabled in this region",
    )

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.FAILED_PRECONDITION


@pytest.mark.parametrize("status", ["INVALID_ARGUMENT", ""])
@pytest.mark.parametrize(
    "message,expected",
    [
        ("API key not valid", AIProviderResponseReason.AUTH),
        (
            "permission denied for model",
            AIProviderResponseReason.PERMISSION_DENIED,
        ),
        (
            "request blocked by safety filter",
            AIProviderResponseReason.INPUT_BLOCKED,
        ),
        ("blocked content", AIProviderResponseReason.INPUT_BLOCKED),
        ("malformed request body", AIProviderResponseReason.INVALID_REQUEST),
    ],
)
def test_invalid_argument_branches_by_message(
    status: str, message: str, expected: AIProviderResponseReason
) -> None:
    """``status`` が空でも ``code=400`` だけで同じ分岐に入る。"""
    exc = _client_error(code=400, status=status, message=message)

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is expected
    assert translated.http_error.status_code == 400


def test_legacy_api_error_unauthenticated_is_classified() -> None:
    """``APIError`` 直接でも分類が成立する。"""
    exc = _api_error(code=401, status="UNAUTHENTICATED", message="invalid key")

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.AUTH


# RESOURCE_EXHAUSTED / code=429 → 流量制限か利用枠の枯渇か
#
# Gemini の 429 は per-minute バーストでも per-day 枯渇でも message が同一文言
# (実際のレスポンス例) のため、以下の fixture は全ケースで message を固定し、
# 構造化 details の quotaId だけが分類を左右することを固定する。

_PER_DAY_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
_PER_MINUTE_QUOTA_ID = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
_QUOTA_EXCEEDED_MESSAGE = (
    "You exceeded your current quota, please check your plan and billing details."
)


def _quota_failure_detail(*quota_ids: str) -> dict:
    """AIP-193 QuotaFailure 型の details 要素 (violations に quotaId を積む)。"""
    return {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": quota_id} for quota_id in quota_ids],
    }


def _resource_exhausted_error(
    *,
    quota_ids: tuple[str, ...] = (),
    details: list[dict] | None = None,
    status: str = "RESOURCE_EXHAUSTED",
    message: str = _QUOTA_EXCEEDED_MESSAGE,
    flat_envelope: bool = False,
) -> genai_errors.ClientError:
    """429 RESOURCE_EXHAUSTED の実レスポンス形を構築する。

    ``quota_ids`` は単一 QuotaFailure の violations として積む簡易 helper。複数
    QuotaFailure item や RetryInfo 混在等、より複雑な details 形は ``details`` を
    直接渡す。``flat_envelope=True`` で ``{"error": {...}}`` ではなく flat 形
    (details 自体が error 相当) を構築する。
    """
    if details is None and quota_ids:
        details = [_quota_failure_detail(*quota_ids)]
    error_body: dict = {"status": status, "message": message}
    if details is not None:
        error_body["details"] = details
    response_json = error_body if flat_envelope else {"error": error_body}
    return genai_errors.ClientError(429, response_json)


@pytest.mark.parametrize("status", ["RESOURCE_EXHAUSTED", ""])
@pytest.mark.parametrize(
    "quota_ids,expected",
    [
        ((), AIProviderResponseReason.RATE_LIMITED),
        ((_PER_MINUTE_QUOTA_ID,), AIProviderResponseReason.RATE_LIMITED),
        ((_PER_DAY_QUOTA_ID,), AIProviderResponseReason.QUOTA_EXHAUSTED),
        (("SomeOtherLimit-FreeTier",), AIProviderResponseReason.RATE_LIMITED),
    ],
)
def test_resource_exhausted_branches_by_quota_id(
    status: str, quota_ids: tuple[str, ...], expected: AIProviderResponseReason
) -> None:
    """同一 message のまま details の quotaId だけで分岐する。status 空でも同じ。"""
    exc = _resource_exhausted_error(status=status, quota_ids=quota_ids)

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is expected
    assert translated.http_error.status_code == 429


@pytest.mark.parametrize(
    "details",
    [
        [_quota_failure_detail(_PER_MINUTE_QUOTA_ID, _PER_DAY_QUOTA_ID)],
        [
            _quota_failure_detail(_PER_MINUTE_QUOTA_ID),
            _quota_failure_detail(_PER_DAY_QUOTA_ID),
        ],
    ],
    ids=["single_quota_failure_item", "multiple_quota_failure_items"],
)
def test_resource_exhausted_mixed_violations_prioritizes_quota_exhausted(
    details: list[dict],
) -> None:
    """per-day と per-minute が混在しても per-day が 1 件あれば枯渇とする。"""
    exc = _resource_exhausted_error(details=details)

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.QUOTA_EXHAUSTED


@pytest.mark.parametrize(
    "malformed_details",
    [
        "not-a-dict",
        ["not", "a", "dict"],
        {"error": "not-a-dict"},
        {"error": {"details": "not-a-list"}},
        {"error": {"details": {"not": "a-list"}}},
    ],
    ids=[
        "details_not_dict",
        "details_list_not_singleton",
        "error_not_dict",
        "error_details_not_list",
        "error_details_dict",
    ],
)
def test_resource_exhausted_malformed_details_is_rate_limited(
    malformed_details: object,
) -> None:
    """details / error.details が不正形なら流量制限に倒す。"""
    exc = _resource_exhausted_error(quota_ids=())
    object.__setattr__(exc, "details", malformed_details)

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.RATE_LIMITED


@pytest.mark.parametrize("flat_envelope", [False, True], ids=["wrapped", "flat"])
def test_resource_exhausted_per_day_quota_regardless_of_envelope_shape(
    flat_envelope: bool,
) -> None:
    """``{"error": {...}}`` 形と flat 形の両方で per-day violation を検出する。"""
    exc = _resource_exhausted_error(
        quota_ids=(_PER_DAY_QUOTA_ID,), flat_envelope=flat_envelope
    )

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.QUOTA_EXHAUSTED


# 入力長の超過: status guard と文言の一致


_CONTEXT_LENGTH_MESSAGES = [
    "Input exceeds context length of 1048576 tokens.",
    "ERROR: context_length_exceeded",
    "request exceeds the maximum number of tokens allowed",
    "input exceeds the maximum input token count",
    "this exceeds the model's context length",
    "this exceeds the model's maximum context length",
    "input is too long for this model",
    "Input EXCEEDS CONTEXT LENGTH",
]


@pytest.mark.parametrize("message", _CONTEXT_LENGTH_MESSAGES)
def test_context_length_with_invalid_argument_is_input_too_long(message: str) -> None:
    exc = _client_error(code=400, status="INVALID_ARGUMENT", message=message)

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.INPUT_TOO_LONG
    assert translated.http_error.status_code == 400


def test_context_length_with_deadline_exceeded_server_error_is_input_too_long() -> None:
    """5xx の DEADLINE_EXCEEDED でも、入力長の超過はサーバーエラーより先に判定する。"""
    exc = _server_error(
        code=504, status="DEADLINE_EXCEEDED", message="Input exceeds context length"
    )

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.INPUT_TOO_LONG
    assert translated.http_error.status_code == 504


def test_context_length_message_with_unrelated_status_is_not_input_too_long() -> None:
    """status guard が無関係 status を弾く — 文言が一致しても入力長の超過にしない。"""
    exc = _client_error(
        code=429,
        status="RESOURCE_EXHAUSTED",
        message="input exceeds context length somehow",
    )

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.RATE_LIMITED


def test_invalid_argument_without_message_is_invalid_request() -> None:
    """message が None の APIError でも落ちずに分類する。"""
    exc = _client_error(code=400, status="INVALID_ARGUMENT", message="")
    # SDK は空 message を None として保持しうるため、属性を直接 None に書き換える
    object.__setattr__(exc, "message", None)

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.INVALID_REQUEST


# 分類できない例外は同じ instance を返す


def test_unknown_exception_returns_same_instance() -> None:
    """未知例外は加工せず同一 instance を返す (caller の bare re-raise 規約)。"""
    exc = ValueError("totally random")
    assert translate_gemini_error(exc) is exc


def test_unknown_api_status_returns_same_instance() -> None:
    """既知 status いずれにも該当しない APIError も identity 保持で返す。"""
    exc = _client_error(code=418, status="TEAPOT", message="weird")
    assert translate_gemini_error(exc) is exc
