"""Gemini 実装の Content Extractor — Stage 3。

Prompt 文面 / model / gen_config / response schema は ``GeminiExtractionPrompt``
が SSoT。本 class は I/O 駆動 (rate limit + SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig, GenerateContentResponse
from pydantic import ValidationError

from app.analysis.errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    ExtractionResponseInvalidError,
)
from app.analysis.extraction.domain import ExtractionResult
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.envelope import ExtractionCall
from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt
from app.config import settings

logger = structlog.get_logger(__name__)

# Gemini が応答を返さなかった理由のうち、**入力内容そのもの** がプロバイダー
# ポリシーに抵触したケース。再試行 / 別モデルでも通らないため記事 DELETE 対象
# (AIProviderOutputBlockedError, NonRetryableDropArticle)。
_POLICY_BLOCKED_FINISH_REASONS: frozenset[str] = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
)

# context length 超過の判定で APIError.message に含まれうるパターン。
# Gemini の正確な phrasing は時期によって揺れるので大小文字無視で substring match。
_CONTEXT_LENGTH_PATTERNS: tuple[str, ...] = (
    "exceeds context length",
    "context_length_exceeded",
    "exceeds the maximum number of tokens",
    "exceeds the maximum input token",
    "exceeds the model's context length",
    "exceeds the model's maximum context length",
    "input is too long",
)


def _extract_raw_text(response: GenerateContentResponse) -> str:
    """``response.text`` を None-safe で取り出す。"""
    text = response.text
    return text if isinstance(text, str) else ""


def _detect_finish_reason(response: GenerateContentResponse) -> str | None:
    """先頭 candidate の ``finish_reason`` を文字列で返す。

    candidate / finish_reason が None の場合は ``None``。enum のときは
    ``.name`` を、文字列なら そのまま返す。
    """
    candidates = response.candidates or []
    if not candidates:
        return None
    finish = candidates[0].finish_reason
    if finish is None:
        return None
    name = getattr(finish, "name", None)
    return name if isinstance(name, str) else str(finish)


class GeminiExtractor(BaseExtractor):
    """BaseExtractor の Gemini API 実装。"""

    MODEL = GeminiExtractionPrompt.MODEL
    RPM = 100
    RPD = 1500

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def extract(
        self,
        title: str,
        content: str,
    ) -> ExtractionCall:
        """プロンプトを構築し API を呼び出して envelope を返す。"""
        prompt = GeminiExtractionPrompt.render(title=title, content=content)
        return await self._call_once(prompt)

    async def _call_api(self, prompt: str) -> ExtractionCall:
        """Gemini の generate_content API を呼び出し envelope を組み立てる。"""
        response = await self._client.aio.models.generate_content(
            model=GeminiExtractionPrompt.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                **GeminiExtractionPrompt.GEN_CONFIG,
                response_schema=GeminiExtractionPrompt.RESPONSE_SCHEMA,
            ),
        )

        # finish_reason が policy block 系なら Layer 2-A の OutputBlocked を raise
        # (NonRetryableDropArticle、記事 DELETE 対象)
        finish_reason = _detect_finish_reason(response)
        if finish_reason in _POLICY_BLOCKED_FINISH_REASONS:
            raise AIProviderOutputBlockedError(f"blocked by policy: {finish_reason}")

        parsed = response.parsed
        if not isinstance(parsed, ExtractionResult):
            # provider は応答したが Stage 3 schema として消化不可 (Layer 2-B、
            # RetryableError、INLINE_RETRY=True で 1 回 retry 救済)
            raise ExtractionResponseInvalidError(
                f"Gemini did not return ExtractionResult "
                f"(got {type(parsed).__name__}, finish_reason={finish_reason})"
            )
        return ExtractionCall(
            result=parsed,
            raw_response=_extract_raw_text(response),
            prompt_version=GeminiExtractionPrompt.VERSION,
        )

    def _translate_error(self, exc: Exception) -> Exception:
        """Gemini SDK 例外を Layer 2 例外階層に分類する。

        翻訳できないケースは ``exc`` をそのまま return する (``_call_once`` が
        bare re-raise → Task 層 catch-all で UNKNOWN ラベル)。
        """
        if isinstance(exc, ValidationError):
            # Pydantic ValidationError は Layer 2-B (Stage 3 工程エラー)
            # provider は応答を返したが Stage 3 が要求する schema を満たさなかった
            return ExtractionResponseInvalidError(
                f"Invalid extraction result schema: {exc}"
            )

        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                # SDK の生 message には key prefix / Authorization header が
                # 含まれる経路があるため固定文言に丸める (red-team chain γ-1)。
                # 詳細 debug は error_chain (SDK class FQN) で代替。
                return AIProviderConfigurationError(
                    "Gemini API key has been reported as leaked; rotate immediately"
                )

            if status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "FAILED_PRECONDITION",
                "NOT_FOUND",
            ):
                return AIProviderConfigurationError(f"{status}: {message}")

            if status in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
                # context length 超過は内容起因 Permanent (DROP_ARTICLE 対象)。
                # message 末尾に詳細 JSON が embed されるため大小文字無視で
                # substring 判定する。
                lowered = message.lower()
                if any(pat in lowered for pat in _CONTEXT_LENGTH_PATTERNS):
                    return AIProviderInputRejectedError(
                        f"input exceeds context length: {message}"
                    )
                # その他の INVALID_ARGUMENT は Stage 3 として消化不可な request
                # bug。RequestInvalid (NonRetryableKeepArticle、運用者がコード/SDK を
                # 直す)
                return AIProviderRequestInvalidError(f"{status}: {message}")

            if status == "RESOURCE_EXHAUSTED":
                # RPM / RPD を message で判定し分けるのは fragile。Gemini の
                # RESOURCE_EXHAUSTED は Rate Limited として扱う (RPD は taskiq
                # RateLimiter の事前チェックで防いでいる、ここに来るのは provider
                # 側の rate)
                return AIProviderRateLimitedError(f"{status}: {message}")

            if isinstance(exc, ServerError):
                return AIProviderServiceUnavailableError(f"{status}: {message}")

            # 既知の APIError status いずれにも該当しない → 翻訳不可
            # (catch-all で UNKNOWN ラベル)
            return exc

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")

        # 想定外の SDK 例外は raw のまま return し、catch-all (UNKNOWN) で受けさせる
        return exc
