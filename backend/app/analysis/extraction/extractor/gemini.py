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
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.extraction.domain import ExtractionResult
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.envelope import ExtractionCall
from app.analysis.extraction.extractor.errors import (
    ExtractionInputTooLargeError,
    ExtractionPolicyBlockedError,
)
from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt
from app.config import settings

logger = structlog.get_logger(__name__)

# Gemini が応答を返さなかった理由のうち、**入力内容そのもの** がプロバイダー
# ポリシーに抵触したケース。再試行 / 別モデルでも通らないため記事 DELETE 対象。
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
            raise ConfigurationError("GEMINI_API_KEY is not configured")
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

        # finish_reason が policy block 系なら parsed が出る前に raise
        finish_reason = _detect_finish_reason(response)
        if finish_reason in _POLICY_BLOCKED_FINISH_REASONS:
            raise ExtractionPolicyBlockedError(
                finish_reason=finish_reason,
                raw_response=_extract_raw_text(response),
                prompt_version=GeminiExtractionPrompt.VERSION,
            )

        parsed = response.parsed
        if not isinstance(parsed, ExtractionResult):
            raise ProviderError(
                f"Gemini did not return ExtractionResult (got {type(parsed).__name__}, "
                f"finish_reason={finish_reason})"
            )
        return ExtractionCall(
            result=parsed,
            raw_response=_extract_raw_text(response),
            prompt_version=GeminiExtractionPrompt.VERSION,
        )

    def _translate_error(self, exc: Exception) -> Exception:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, ValidationError):
            return ProviderError(f"Invalid extraction result schema: {exc}")

        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                return ConfigurationError(f"API key leaked: {message}")

            if status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "FAILED_PRECONDITION",
                "NOT_FOUND",
            ):
                return ConfigurationError(f"{status}: {message}")

            if status in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
                # context length 超過は内容起因 Permanent (DELETE 対象)。
                # message 末尾に詳細 JSON が embed されるため大小文字無視で
                # substring 判定する。
                lowered = message.lower()
                if any(pat in lowered for pat in _CONTEXT_LENGTH_PATTERNS):
                    return ExtractionInputTooLargeError(
                        prompt_version=GeminiExtractionPrompt.VERSION,
                    )
                return InvalidInputError(f"{status}: {message}")

            if status == "RESOURCE_EXHAUSTED":
                return RateLimitError(f"{status}: {message}")

            if isinstance(exc, ServerError):
                return ProviderError(f"{status}: {message}")

            return UnclassifiedError(
                f"Unhandled APIError {exc.code} {status}: {message}"
            )

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        return UnclassifiedError(f"{type(exc).__name__}: {exc}")
