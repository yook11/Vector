"""Gemini SDK 例外を AIProvider*Error に分類する共通 translator。

analysis pipeline 内 (Stage 3 curation / Stage 4 assessment / Stage 5 embedding) で、
同じ Gemini 障害が Stage 違いで別の AIProvider*Error に分類されないようにする。

責任の境界:
- ``translate_gemini_error``: SDK exception オブジェクトを見た分類のみ。
- ``is_context_length_error``: Stage 3 specific 判定。Stage 3 は INVALID_ARGUMENT を
  「入力長超過」と「その他のリクエスト不正」に分けたいので、message pattern を
  ここに集約する。

reason 語彙の所有:
- ``GeminiContentRejectionReason``: 入出力 content が弾かれた具体理由 (finish_reason
  由来の safety / recitation 等、入力 block、入力長超過)。検知箇所 (本 translator と
  各 gemini adapter) が共有する語彙なので、provider 非依存な本 module が所有する。
- ``GeminiStateReason``: provider / 環境状態の具体理由 (timeout / server_error /
  auth 等)。translator の各分岐 + gemini adapter local 検知 (未設定 / 空応答) と
  対応する。reason は audit の ``failure_reason`` に焼かれる種別ラベル (PII-free)。

Stage 固有の以下は translator に持ち込まない (stage-local で扱う):
- finish_reason 判定 (SDK response attribute 由来、exception ではない)
- ValidationError / JSONDecodeError / response shape 違反 (Layer 2-B、stage 責任)
"""

from __future__ import annotations

from enum import StrEnum

import httpx
from google.genai import errors as genai_errors

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)


class GeminiContentRejectionReason(StrEnum):
    """Gemini が入出力 content を弾いた具体理由 (PII-free な種別ラベル)。

    finish_reason 由来 (出力ブロック) と入力起因 (入力 block / 入力長超過) を
    包含する。値は audit の ``failure_reason`` に焼かれ、``outcome_code``
    (= CODE) より細かい原因を残す。
    """

    SAFETY = "safety"
    RECITATION = "recitation"
    BLOCKLIST = "blocklist"
    PROHIBITED_CONTENT = "prohibited_content"
    SPII = "spii"
    INPUT_BLOCKED = "input_blocked"
    CONTEXT_LENGTH = "context_length"


class GeminiStateReason(StrEnum):
    """Gemini provider / 環境状態の具体理由 (PII-free な種別ラベル)。

    translator の各分岐 (timeout / server_error / auth 等) と gemini adapter local
    検知 (未設定 / 空応答) に対応する。同一 ``outcome_code`` に複数の状態が畳まれる
    ケース (例: configuration = leaked_api_key / auth / not_found ...) を
    ``failure_reason`` で区別できるようにする。
    """

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER_ERROR = "server_error"
    LEAKED_API_KEY = "leaked_api_key"
    AUTH = "auth"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    FAILED_PRECONDITION = "failed_precondition"
    INVALID_ARGUMENT = "invalid_argument"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    # gemini adapter local 検知 (translator 分岐外)。
    NOT_CONFIGURED = "not_configured"
    STREAM_TRUNCATED = "stream_truncated"
    EMPTY_EMBEDDINGS = "empty_embeddings"
    MISSING_VALUES = "missing_values"
    EMBEDDING_COUNT_MISMATCH = "embedding_count_mismatch"


# finish_reason 名 → content 拒否 reason。出力ブロックを検知した adapter が、
# 自身の blocked-set に含めた finish_reason 名を本写像で reason に変換する。
_FINISH_REASON_TO_CONTENT_REASON: dict[str, GeminiContentRejectionReason] = {
    "SAFETY": GeminiContentRejectionReason.SAFETY,
    "RECITATION": GeminiContentRejectionReason.RECITATION,
    "BLOCKLIST": GeminiContentRejectionReason.BLOCKLIST,
    "PROHIBITED_CONTENT": GeminiContentRejectionReason.PROHIBITED_CONTENT,
    "SPII": GeminiContentRejectionReason.SPII,
}


# gRPC status → configuration reason。``configuration`` CODE に畳まれる複数状態を
# forensics 用に区別する。
_STATUS_TO_CONFIG_REASON: dict[str, GeminiStateReason] = {
    "UNAUTHENTICATED": GeminiStateReason.AUTH,
    "PERMISSION_DENIED": GeminiStateReason.PERMISSION_DENIED,
    "NOT_FOUND": GeminiStateReason.NOT_FOUND,
    "FAILED_PRECONDITION": GeminiStateReason.FAILED_PRECONDITION,
}


# HTTP code (gRPC status 不在の envelope 用) → configuration reason。
_HTTP_CODE_TO_CONFIG_REASON: dict[int, GeminiStateReason] = {
    401: GeminiStateReason.AUTH,
    403: GeminiStateReason.PERMISSION_DENIED,
    404: GeminiStateReason.NOT_FOUND,
}


def output_blocked_reason(finish_reason_name: str) -> GeminiContentRejectionReason:
    """blocked-set に含まれる finish_reason 名を content 拒否 reason に写す。

    呼び出し側 (adapter) が自身の blocked-set で先に絞る前提なので、写像に無い
    名前は契約違反として ``KeyError`` で fail-fast する (silent 丸めを避ける)。
    """
    return _FINISH_REASON_TO_CONTENT_REASON[finish_reason_name]


# Stage 3 で「入力長超過」を InputRejected として表現するための message pattern。
# Stage 4/5 では context-length 越えは起きない想定なので、is_context_length_error は
# Stage 3 のみが参照する。Pattern は Gemini SDK が返した過去の実例から拡張可能。
_CONTEXT_LENGTH_PATTERNS: tuple[str, ...] = (
    "exceeds context length",
    "context_length_exceeded",
    "exceeds the maximum number of tokens",
    "exceeds the maximum input token",
    "exceeds the model's context length",
    "exceeds the model's maximum context length",
    "input is too long",
)


def is_context_length_error(exc: Exception) -> bool:
    """``exc`` が「入力長超過」を示す Gemini SDK 例外かどうか。

    Stage 3 GeminiCurator が ``INVALID_ARGUMENT`` / ``DEADLINE_EXCEEDED`` のうち
    入力長超過だけを ``AIProviderInputRejectedError`` に振り分けるために使う。
    status guard を入れることで、無関係 status の APIError の message に偶然
    pattern が含まれた場合の誤分類を防ぐ。
    """
    if not isinstance(exc, genai_errors.APIError):
        return False
    status = getattr(exc, "status", None) or ""
    if status not in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
        return False
    message = (getattr(exc, "message", None) or str(exc) or "").lower()
    return any(pat in message for pat in _CONTEXT_LENGTH_PATTERNS)


def translate_gemini_error(exc: Exception) -> Exception:
    """Gemini SDK 例外を ``AIProvider*Error`` に分類する。

    分類優先順位:

    1. Network (httpx + builtin)
    2. ServerError (5xx) → ServiceUnavailable
    3. APIError (4xx 系の親型):

       a. leaked key 固定文言
       b. **設定系 status** (UNAUTHENTICATED/PERMISSION_DENIED/NOT_FOUND/
          FAILED_PRECONDITION) → ConfigurationError
       c. 設定系 HTTP code (401/403/404) → ConfigurationError
       d. INVALID_ARGUMENT / code=400 の 3-way 分岐
       e. RESOURCE_EXHAUSTED / code=429 の 2-way 分岐 (usage limit vs rate)
    4. unknown → return exc

    各分岐は ``reason`` (GeminiStateReason / GeminiContentRejectionReason) を付与し、
    同一 CODE に畳まれる複数状態を audit で区別できるようにする。

    設計判断: 設定系 status を INVALID_ARGUMENT 分岐より **先に** 評価する。
    ``code=400`` と ``status=FAILED_PRECONDITION`` が同居する envelope では、
    status (gRPC semantics) を優先する方が分類が安定する (HTTP code は
    SDK のバージョン差で揺れる、status は API 設計上の semantics)。

    未知の例外はそのまま返す (呼び出し側で stage-specific 判定があるため、
    translator は SDK 例外オブジェクトだけを引き受ける)。
    """
    # Network errors。SDK 生 message を渡さず、PII 含有経路を残さない。
    # timeout / connection を reason で区別する。
    if isinstance(exc, httpx.TimeoutException):
        return AIProviderNetworkError(reason=GeminiStateReason.TIMEOUT)
    if isinstance(exc, httpx.ConnectError):
        return AIProviderNetworkError(reason=GeminiStateReason.CONNECTION)
    if isinstance(exc, TimeoutError):
        return AIProviderNetworkError(reason=GeminiStateReason.TIMEOUT)
    if isinstance(exc, ConnectionError | OSError):
        return AIProviderNetworkError(reason=GeminiStateReason.CONNECTION)

    # 2. ServerError は APIError subclass なので先に判定して 5xx を分離する。
    if isinstance(exc, genai_errors.ServerError):
        return AIProviderServiceUnavailableError(reason=GeminiStateReason.SERVER_ERROR)

    # 3. APIError は ClientError / ServerError の共通親型。ServerError を
    #    先に処理済みなので、ここに来るのは 4xx 系。google-genai 1.x の APIError は
    #    HTTP code (``code``) と gRPC status (``status``) を両方持ち得るので両方判定。
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None) or ""
        raw_message = str(getattr(exc, "message", "")) or str(exc)
        message = raw_message.lower()

        # 3a. Leaked API key は固定文言だけを見て、SDK 生 message は外へ出さない。
        if "reported as leaked" in message:
            return AIProviderConfigurationError(reason=GeminiStateReason.LEAKED_API_KEY)

        # 3b. 設定系 status を **先に** 評価。FAILED_PRECONDITION が同時に
        #     code=400 を持っていても ConfigurationError に振り分ける。
        if status in _STATUS_TO_CONFIG_REASON:
            return AIProviderConfigurationError(reason=_STATUS_TO_CONFIG_REASON[status])

        # 3c. 設定系 HTTP code (gRPC status が空の envelope 用)。
        if code in _HTTP_CODE_TO_CONFIG_REASON:
            return AIProviderConfigurationError(
                reason=_HTTP_CODE_TO_CONFIG_REASON[code]
            )

        # 3d. 400 / INVALID_ARGUMENT を 3-way 分岐。
        if code == 400 or status == "INVALID_ARGUMENT":
            if "api key" in message:
                return AIProviderConfigurationError(reason=GeminiStateReason.AUTH)
            if "permission" in message:
                return AIProviderConfigurationError(
                    reason=GeminiStateReason.PERMISSION_DENIED
                )
            if "blocked" in message or "safety" in message:
                return AIProviderInputRejectedError(
                    reason=GeminiContentRejectionReason.INPUT_BLOCKED
                )
            return AIProviderRequestInvalidError(
                reason=GeminiStateReason.INVALID_ARGUMENT
            )

        # 3e. 429 / RESOURCE_EXHAUSTED。quota/daily message は利用枠 exhausted
        #     として、rate limit (一時的バースト超過) と区別する。
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            if "quota" in message or "daily" in message:
                return AIProviderUsageLimitExhaustedError(
                    reason=GeminiStateReason.QUOTA_EXHAUSTED
                )
            return AIProviderRateLimitedError(reason=GeminiStateReason.RATE_LIMITED)

    # 4. 未知。stage-local 側で stage-specific 判定 (ValidationError 等) を
    #    補えるよう、加工せず返す。
    return exc
