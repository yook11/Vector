"""Gemini SDK 例外を AIProvider*Error に分類する共通 translator。

analysis pipeline 内 (Stage 3 extraction / Stage 4 assessment / Stage 5 embedding) で、
同じ Gemini 障害が Stage 違いで別の AIProvider*Error に分類されないようにする。

責任の境界:
- ``translate_gemini_error``: SDK exception オブジェクトを見た分類のみ。
- ``is_context_length_error``: Stage 3 specific 判定。Stage 3 は INVALID_ARGUMENT を
  「入力長超過」と「その他のリクエスト不正」に分けたいので、message pattern を
  ここに集約する。

Stage 固有の以下は translator に持ち込まない (stage-local で扱う):
- finish_reason 判定 (SDK response attribute 由来、exception ではない)
- ValidationError / JSONDecodeError / response shape 違反 (Layer 2-B、stage 責任)

Search BC (``app.search.embedding.gemini.GeminiQueryEmbedder``) は memory
``feedback_no_share_different_problems`` により独立 hierarchy として複製されており、
本 module は import しない。
"""

from __future__ import annotations

import httpx
from google.genai import errors as genai_errors

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)

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

    Stage 3 GeminiExtractor が ``INVALID_ARGUMENT`` / ``DEADLINE_EXCEEDED`` のうち
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
       e. RESOURCE_EXHAUSTED / code=429 の 2-way 分岐 (quota vs rate)
    4. unknown → return exc

    設計判断: 設定系 status を INVALID_ARGUMENT 分岐より **先に** 評価する。
    ``code=400`` と ``status=FAILED_PRECONDITION`` が同居する envelope では、
    status (gRPC semantics) を優先する方が分類が安定する (HTTP code は
    SDK のバージョン差で揺れる、status は API 設計上の semantics)。

    未知の例外はそのまま返す (呼び出し側で stage-specific 判定があるため、
    translator は SDK 例外オブジェクトだけを引き受ける)。
    """
    # 1. Network errors (httpx + builtin)。SDK transport の httpx 経由でも到達する。
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError):
        return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")

    # 2. ServerError は APIError subclass なので先に判定して 5xx を分離する。
    if isinstance(exc, genai_errors.ServerError):
        return AIProviderServiceUnavailableError(str(exc))

    # 3. APIError は ClientError / ServerError の共通親型。ServerError を
    #    先に処理済みなので、ここに来るのは 4xx 系。google-genai 1.x の APIError は
    #    HTTP code (``code``) と gRPC status (``status``) を両方持ち得るので両方判定。
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None) or ""
        raw_message = str(getattr(exc, "message", "")) or str(exc)
        message = raw_message.lower()

        # 3a. Leaked API key は内部 detail を露出しない固定文言にする
        #     (red-team chain γ-1: SDK 生 message に key prefix を含む経路があるため)。
        if "reported as leaked" in message:
            return AIProviderConfigurationError(
                "Gemini API key has been reported as leaked; rotate immediately"
            )

        # 3b. 設定系 status を **先に** 評価。FAILED_PRECONDITION が同時に
        #     code=400 を持っていても ConfigurationError に振り分ける。
        if status in (
            "UNAUTHENTICATED",
            "PERMISSION_DENIED",
            "NOT_FOUND",
            "FAILED_PRECONDITION",
        ):
            return AIProviderConfigurationError(str(exc))

        # 3c. 設定系 HTTP code (gRPC status が空の envelope 用)。
        if code in (401, 403, 404):
            return AIProviderConfigurationError(str(exc))

        # 3d. 400 / INVALID_ARGUMENT を 3-way 分岐。
        if code == 400 or status == "INVALID_ARGUMENT":
            if "api key" in message or "permission" in message:
                return AIProviderConfigurationError(str(exc))
            if "blocked" in message or "safety" in message:
                return AIProviderInputRejectedError(str(exc))
            return AIProviderRequestInvalidError(str(exc))

        # 3e. 429 / RESOURCE_EXHAUSTED。quota/daily message は Quota 系として
        #     rate limit (一時的バースト超過) と区別する (運用上 retry 戦略が違う)。
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            if "quota" in message or "daily" in message:
                return AIProviderQuotaExhaustedError(str(exc))
            return AIProviderRateLimitedError(str(exc))

    # 4. 未知。stage-local 側で stage-specific 判定 (ValidationError 等) を
    #    補えるよう、加工せず返す。
    return exc
