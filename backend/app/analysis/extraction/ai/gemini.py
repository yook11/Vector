"""Gemini 実装の Content Extractor — Stage 3。

Prompt 文面 / model / gen_config / response schema は ``GeminiExtractionPrompt``
が SSoT。本 class は I/O 駆動 (rate limit + SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.types import GenerateContentConfig, GenerateContentResponse
from pydantic import ValidationError

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.ai.gemini_prompt import GeminiExtractionPrompt
from app.analysis.extraction.ai.parse import parse_extraction
from app.analysis.extraction.ai.schema import GeminiExtractionResponse
from app.analysis.extraction.domain import Noise, Signal
from app.analysis.extraction.errors import ExtractionResponseInvalidError
from app.analysis.gemini_error_translator import (
    is_context_length_error,
    translate_gemini_error,
)
from app.config import settings

logger = structlog.get_logger(__name__)

# Gemini が応答を返さなかった理由のうち、**入力内容そのもの** がプロバイダー
# ポリシーに抵触したケース。再試行 / 別モデルでも通らないため記事 DELETE 対象
# (AIProviderOutputBlockedError, NonRetryableDropArticle)。
_POLICY_BLOCKED_FINISH_REASONS: frozenset[str] = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
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

    PROVIDER = "gemini"
    MODEL = GeminiExtractionPrompt.MODEL
    PROMPT_VERSION = GeminiExtractionPrompt.VERSION
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
    ) -> ExtractionCall[Signal] | ExtractionCall[Noise]:
        """プロンプトを構築し API を呼び出して envelope を返す。"""
        prompt = GeminiExtractionPrompt.render(title=title, content=content)
        return await self._call_once(prompt)

    async def _call_api(
        self, prompt: str
    ) -> ExtractionCall[Signal] | ExtractionCall[Noise]:
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
        if not isinstance(parsed, GeminiExtractionResponse):
            # provider は応答したが Stage 3 schema として消化不可 (Layer 2-B、
            # RetryableError、INLINE_RETRY=True で 1 回 retry 救済)
            raise ExtractionResponseInvalidError(
                f"Gemini did not return GeminiExtractionResponse "
                f"(got {type(parsed).__name__}, finish_reason={finish_reason})"
            )
        result = parse_extraction(parsed)
        return ExtractionCall(
            result=result,
            raw_response=_extract_raw_text(response),
            raw_relevance=parsed.relevance,
            prompt_version=GeminiExtractionPrompt.VERSION,
            model_name=GeminiExtractionPrompt.MODEL,
        )

    def _translate_error(self, exc: Exception) -> Exception:
        """SDK / Pydantic 例外を Layer 2 例外階層に翻訳する。

        Stage 3 specific:

        - ``ValidationError``: schema validation 失敗 (Layer 2-B、retryable)。
        - 入力長超過 (``INVALID_ARGUMENT`` + context-length message):
          ``AIProviderInputRejectedError`` として、Stage 4/5 と違うバリエーション
          (=「入力が長すぎる」semantics) を保持する。

        その他の SDK 例外分類は ``translate_gemini_error`` に委譲する。
        """
        if isinstance(exc, ValidationError):
            return ExtractionResponseInvalidError(
                f"Invalid extraction result schema: {exc}"
            )
        if is_context_length_error(exc):
            msg = getattr(exc, "message", None) or str(exc)
            return AIProviderInputRejectedError(f"input exceeds context length: {msg}")
        return translate_gemini_error(exc)
