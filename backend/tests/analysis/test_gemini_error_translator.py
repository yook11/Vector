"""``app.analysis.gemini_error_translator`` の golden table テスト。

analysis pipeline 内で共有される SDK 例外 → ``AIProvider*Error`` 分類を検証する。
ValidationError / response shape / finish_reason など stage 固有の判定は対象外。

``is_context_length_error`` は status guard (INVALID_ARGUMENT / DEADLINE_EXCEEDED)
を持つため、無関係 status の APIError に偶然 pattern が混入しても誤分類しない
ことを negative test で担保する。
"""

from __future__ import annotations

import httpx
import pytest
from google.genai import errors as genai_errors

import app.analysis.ai_provider_errors as ai_provider_errors
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.gemini_error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
    is_context_length_error,
    translate_gemini_error,
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


# Network 系 (httpx + builtin)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.TimeoutException("timed out"),
        httpx.ConnectError("connection refused"),
        TimeoutError("io timeout"),
        ConnectionError("conn reset"),
        OSError("dns failure"),
    ],
)
def test_network_errors_translate_to_network_error(exc: Exception) -> None:
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderNetworkError)
    assert translated.CODE == "ai_error_network"


# ServerError (5xx) → ServiceUnavailable


def test_server_error_translates_to_service_unavailable() -> None:
    translated = translate_gemini_error(_server_error())
    assert isinstance(translated, AIProviderServiceUnavailableError)
    assert translated.CODE == "ai_error_service_unavailable"


# Leaked API key → ConfigurationError (固定文言)


def test_leaked_key_message_is_fixed_string_not_sdk_echo() -> None:
    """key prefix を含む SDK message が ``__str__`` に出ない。

    translator は ``AIProviderConfigurationError()`` を引数なしで返し、
    ``__str__`` は ``CODE='ai_error_configuration'`` のみを返す。
    """
    sdk_message = (
        "API key AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q has been "
        "reported as leaked at https://github.com/foo/bar"
    )
    exc = _client_error(code=400, status="INVALID_ARGUMENT", message=sdk_message)

    translated = translate_gemini_error(exc)

    assert isinstance(translated, AIProviderConfigurationError)
    # SAFE_ATTRS 経路で SDK 生 message が一切残らない構造的契約 (PII 隔離)
    assert "AIza" not in str(translated)
    assert "github.com" not in str(translated)
    assert "ai_error_configuration" in str(translated)


# 設定系 status / HTTP code → ConfigurationError


@pytest.mark.parametrize(
    "code,status",
    [
        (401, "UNAUTHENTICATED"),
        (403, "PERMISSION_DENIED"),
        (404, "NOT_FOUND"),
        (400, "FAILED_PRECONDITION"),  # status 優先評価の確認
    ],
)
def test_config_status_translates_to_configuration_error(
    code: int, status: str
) -> None:
    exc = _client_error(code=code, status=status, message="config issue")
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)


@pytest.mark.parametrize("code", [401, 403, 404])
def test_config_http_code_without_status_translates_to_configuration_error(
    code: int,
) -> None:
    """status が空でも HTTP code (401/403/404) で ConfigurationError に振る。"""
    exc = _client_error(code=code, status="", message="config")
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)


def test_failed_precondition_with_code_400_evaluates_status_first() -> None:
    """``code=400`` と ``status=FAILED_PRECONDITION`` が同居しても status 優先。

    INVALID_ARGUMENT 分岐より前に設定系 status を評価することを構造的に固定。
    gRPC status は API semantics で安定、HTTP code は SDK 差分で揺れる。
    """
    exc = _client_error(
        code=400,
        status="FAILED_PRECONDITION",
        message="model not enabled in this region",
    )
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)


# INVALID_ARGUMENT / code=400 → 3-way 分岐


@pytest.mark.parametrize(
    "message,expected",
    [
        ("API key not valid", AIProviderConfigurationError),
        ("permission denied for model", AIProviderConfigurationError),
        ("request blocked by safety filter", AIProviderInputRejectedError),
        ("blocked content", AIProviderInputRejectedError),
        ("malformed request body", AIProviderRequestInvalidError),
    ],
)
def test_invalid_argument_status_3way_branch(message: str, expected: type) -> None:
    exc = _client_error(code=400, status="INVALID_ARGUMENT", message=message)
    translated = translate_gemini_error(exc)
    assert isinstance(translated, expected)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("API key not valid", AIProviderConfigurationError),
        ("request blocked by safety filter", AIProviderInputRejectedError),
        ("malformed request body", AIProviderRequestInvalidError),
    ],
)
def test_code_400_without_status_3way_branch(message: str, expected: type) -> None:
    """``status`` 空でも ``code=400`` だけで同じ 3-way 分岐に入る。"""
    exc = _client_error(code=400, status="", message=message)
    translated = translate_gemini_error(exc)
    assert isinstance(translated, expected)


def test_input_blocked_translation_marks_provider_neutral_safety_kind() -> None:
    rejection_kind = getattr(
        ai_provider_errors,
        "AIProviderContentRejectionKind",
        None,
    )
    assert rejection_kind is not None
    error = _client_error(
        code=400,
        status="INVALID_ARGUMENT",
        message="request blocked by safety filter",
    )

    translated = translate_gemini_error(error)

    assert isinstance(translated, AIProviderInputRejectedError)
    assert translated.CODE == "ai_error_input_rejected"
    assert translated.reason is GeminiContentRejectionReason.INPUT_BLOCKED
    assert translated.rejection_kind is rejection_kind.SAFETY
    assert translated.is_safety_rejection is True


# RESOURCE_EXHAUSTED / code=429 → 2-way 分岐 (usage limit vs rate)
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


@pytest.mark.parametrize(
    "quota_ids,expected",
    [
        ((), AIProviderRateLimitedError),
        ((_PER_MINUTE_QUOTA_ID,), AIProviderRateLimitedError),
        ((_PER_DAY_QUOTA_ID,), AIProviderUsageLimitExhaustedError),
    ],
)
def test_resource_exhausted_status_2way_branch(
    quota_ids: tuple[str, ...], expected: type
) -> None:
    """同一 message のまま details の quotaId だけで分岐する。"""
    exc = _resource_exhausted_error(quota_ids=quota_ids)
    translated = translate_gemini_error(exc)
    assert isinstance(translated, expected)


def test_code_429_without_status_routes_to_rate_or_quota() -> None:
    """``status`` 空でも ``code=429`` で同じ details ベース分岐に入る。"""
    quota_exc = _resource_exhausted_error(status="", quota_ids=(_PER_DAY_QUOTA_ID,))
    rate_exc = _resource_exhausted_error(status="", quota_ids=())
    assert isinstance(
        translate_gemini_error(quota_exc),
        AIProviderUsageLimitExhaustedError,
    )
    assert isinstance(translate_gemini_error(rate_exc), AIProviderRateLimitedError)


# 3e: positive allowlist の詳細契約 — details 構造だけで分類する (message は無関係)


def test_resource_exhausted_per_minute_quota_only_classifies_as_rate_limited() -> None:
    exc = _resource_exhausted_error(quota_ids=(_PER_MINUTE_QUOTA_ID,))
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)


def test_resource_exhausted_per_day_quota_only_classifies_as_exhausted() -> None:
    exc = _resource_exhausted_error(quota_ids=(_PER_DAY_QUOTA_ID,))
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderUsageLimitExhaustedError)


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
def test_resource_exhausted_mixed_violations_prioritizes_usage_limit_exhausted(
    details: list[dict],
) -> None:
    """per-day と per-minute が混在しても per-day が 1 件あれば usage limit 優先。"""
    exc = _resource_exhausted_error(details=details)
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderUsageLimitExhaustedError)


def test_resource_exhausted_without_details_classifies_as_rate_limited() -> None:
    """details 欠損 (SDK が返さない/剥落) は rate limited に倒す。"""
    exc = _resource_exhausted_error(quota_ids=())
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)


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
def test_resource_exhausted_malformed_details_classifies_as_rate_limited(
    malformed_details: object,
) -> None:
    """details / error.details が不正形なら rate limited に倒す。"""
    exc = _resource_exhausted_error(quota_ids=())
    object.__setattr__(exc, "details", malformed_details)
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)


def test_resource_exhausted_unknown_quota_id_classifies_as_rate_limited() -> None:
    """per-day を含まない未知 quotaId は rate limited に倒す。"""
    exc = _resource_exhausted_error(quota_ids=("SomeOtherLimit-FreeTier",))
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)


@pytest.mark.parametrize("flat_envelope", [False, True], ids=["wrapped", "flat"])
def test_resource_exhausted_per_day_quota_classifies_regardless_of_envelope_shape(
    flat_envelope: bool,
) -> None:
    """``{"error": {...}}`` 形と flat 形の両方で per-day violation を検出する。"""
    exc = _resource_exhausted_error(
        quota_ids=(_PER_DAY_QUOTA_ID,), flat_envelope=flat_envelope
    )
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderUsageLimitExhaustedError)


def test_legacy_api_error_unauthenticated_classifies_as_configuration() -> None:
    """``APIError`` 直接でも分類が成立する。"""
    exc = _api_error(code=401, status="UNAUTHENTICATED", message="invalid key")
    translated = translate_gemini_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)


# 各分岐が原因詳細 reason を自己記述する (起きた箇所が reason を上げる)


@pytest.mark.parametrize(
    "exc,expected_cls,expected_reason",
    [
        (
            httpx.TimeoutException("t"),
            AIProviderNetworkError,
            GeminiStateReason.TIMEOUT,
        ),
        (
            httpx.ConnectError("c"),
            AIProviderNetworkError,
            GeminiStateReason.CONNECTION,
        ),
        (TimeoutError("t"), AIProviderNetworkError, GeminiStateReason.TIMEOUT),
        (ConnectionError("c"), AIProviderNetworkError, GeminiStateReason.CONNECTION),
        (OSError("dns"), AIProviderNetworkError, GeminiStateReason.CONNECTION),
        (
            _server_error(),
            AIProviderServiceUnavailableError,
            GeminiStateReason.SERVER_ERROR,
        ),
        (
            _client_error(
                code=400,
                status="INVALID_ARGUMENT",
                message=(
                    "API key AIza... has been reported as leaked at "
                    "https://github.com/foo"
                ),
            ),
            AIProviderConfigurationError,
            GeminiStateReason.LEAKED_API_KEY,
        ),
        (
            _client_error(code=401, status="UNAUTHENTICATED"),
            AIProviderConfigurationError,
            GeminiStateReason.AUTH,
        ),
        (
            _client_error(code=403, status="PERMISSION_DENIED"),
            AIProviderConfigurationError,
            GeminiStateReason.PERMISSION_DENIED,
        ),
        (
            _client_error(code=404, status="NOT_FOUND"),
            AIProviderConfigurationError,
            GeminiStateReason.NOT_FOUND,
        ),
        (
            _client_error(code=400, status="FAILED_PRECONDITION"),
            AIProviderConfigurationError,
            GeminiStateReason.FAILED_PRECONDITION,
        ),
        (
            _client_error(
                code=400, status="INVALID_ARGUMENT", message="API key not valid"
            ),
            AIProviderConfigurationError,
            GeminiStateReason.AUTH,
        ),
        (
            _client_error(
                code=400, status="INVALID_ARGUMENT", message="permission denied"
            ),
            AIProviderConfigurationError,
            GeminiStateReason.PERMISSION_DENIED,
        ),
        (
            _client_error(
                code=400,
                status="INVALID_ARGUMENT",
                message="request blocked by safety filter",
            ),
            AIProviderInputRejectedError,
            GeminiContentRejectionReason.INPUT_BLOCKED,
        ),
        (
            _client_error(
                code=400, status="INVALID_ARGUMENT", message="malformed request"
            ),
            AIProviderRequestInvalidError,
            GeminiStateReason.INVALID_ARGUMENT,
        ),
        (
            _resource_exhausted_error(quota_ids=(_PER_DAY_QUOTA_ID,)),
            AIProviderUsageLimitExhaustedError,
            GeminiStateReason.QUOTA_EXHAUSTED,
        ),
        (
            _resource_exhausted_error(quota_ids=()),
            AIProviderRateLimitedError,
            GeminiStateReason.RATE_LIMITED,
        ),
    ],
)
def test_translation_carries_reason(
    exc: Exception, expected_cls: type, expected_reason: object
) -> None:
    """各分岐が CODE (class) に加え原因詳細 reason を自己記述する。"""
    translated = translate_gemini_error(exc)
    assert isinstance(translated, expected_cls)
    assert translated.reason is expected_reason  # type: ignore[attr-defined]


# Unknown → return exc (identity 保持)


def test_unknown_exception_returns_same_instance() -> None:
    """未知例外は加工せず同一 instance を返す (caller の bare re-raise 規約)。"""
    exc = ValueError("totally random")
    assert translate_gemini_error(exc) is exc


def test_unknown_api_status_returns_same_instance() -> None:
    """既知 status いずれにも該当しない APIError も identity 保持で返す。"""
    exc = _client_error(code=418, status="TEAPOT", message="weird")
    assert translate_gemini_error(exc) is exc


# is_context_length_error: status guard と pattern matching


_CONTEXT_LENGTH_MESSAGES = [
    "Input exceeds context length of 1048576 tokens.",
    "ERROR: context_length_exceeded",
    "request exceeds the maximum number of tokens allowed",
    "input exceeds the maximum input token count",
    "this exceeds the model's context length",
    "this exceeds the model's maximum context length",
    "input is too long for this model",
]


@pytest.mark.parametrize("message", _CONTEXT_LENGTH_MESSAGES)
def test_is_context_length_error_positive_invalid_argument(message: str) -> None:
    exc = _client_error(code=400, status="INVALID_ARGUMENT", message=message)
    assert is_context_length_error(exc) is True


def test_is_context_length_error_positive_deadline_exceeded() -> None:
    exc = _client_error(
        code=504,
        status="DEADLINE_EXCEEDED",
        message="Input exceeds context length",
    )
    assert is_context_length_error(exc) is True


def test_is_context_length_error_is_case_insensitive() -> None:
    exc = _client_error(
        code=400, status="INVALID_ARGUMENT", message="Input EXCEEDS CONTEXT LENGTH"
    )
    assert is_context_length_error(exc) is True


def test_is_context_length_error_negative_unrelated_status_with_matching_message() -> (
    None
):
    """status guard が無関係 status を弾く証跡 — pattern 一致しても False。"""
    exc = _client_error(
        code=429,
        status="RESOURCE_EXHAUSTED",
        message="input exceeds context length somehow",
    )
    assert is_context_length_error(exc) is False


def test_is_context_length_error_negative_invalid_argument_no_pattern() -> None:
    exc = _client_error(
        code=400, status="INVALID_ARGUMENT", message="malformed request body"
    )
    assert is_context_length_error(exc) is False


def test_is_context_length_error_negative_non_sdk_exception() -> None:
    assert is_context_length_error(ValueError("anything")) is False


def test_is_context_length_error_negative_none_message() -> None:
    """message が None の APIError でも False (NoneType crash しない)。"""
    exc = _client_error(code=400, status="INVALID_ARGUMENT", message="")
    # SDK は空 message を None として保持しうるため、属性を直接 None に書き換える
    object.__setattr__(exc, "message", None)
    assert is_context_length_error(exc) is False
