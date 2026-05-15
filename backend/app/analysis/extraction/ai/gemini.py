"""Gemini 実装の Content Extractor — Stage 3。

Prompt 文面は ``GeminiExtractionPrompt``、API call spec (model / gen_config /
response_schema / version / rate policy) は ``GeminiExtractionSpec`` singleton
が SSoT。本 class は I/O 駆動 (rate limit + SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

from typing import Final

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
from app.analysis.extraction.ai.gemini_spec import (
    GEMINI_EXTRACTION_SPEC,
    GeminiExtractionSpec,
)
from app.analysis.extraction.ai.parse import parse_extraction
from app.analysis.extraction.ai.schema import GeminiExtractionResponse
from app.analysis.extraction.domain import Noise, Signal
from app.analysis.extraction.errors import ExtractionResponseInvalidError
from app.analysis.gemini_error_translator import (
    is_context_length_error,
    translate_gemini_error,
)
from app.analysis.rate_limit import RatePolicy
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

    SPEC: Final[GeminiExtractionSpec] = GEMINI_EXTRACTION_SPEC

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    # -- BaseExtractor property 契約 --

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def prompt_version(self) -> str:
        return self.SPEC.version

    @property
    def rate_policy(self) -> RatePolicy:
        return self.SPEC.rate_policy

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
            model=self.SPEC.model,
            contents=prompt,
            config=GenerateContentConfig(
                **self.SPEC.gen_config,
                response_schema=self.SPEC.response_schema,
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
        # ``ExtractionCall[T]`` の T は invariant のため Signal | Noise を直接
        # 渡すと ``ExtractionCall[Signal | Noise]`` に推論される。戻り値型は
        # ``ExtractionCall[Signal] | ExtractionCall[Noise]`` なので isinstance で
        # narrow してから明示的に型パラメータを指定する。
        raw_response = _extract_raw_text(response)
        if isinstance(result, Signal):
            return ExtractionCall[Signal](
                result=result,
                raw_response=raw_response,
                raw_relevance=parsed.relevance,
                prompt_version=self.SPEC.version,
                model_name=self.SPEC.model,
            )
        return ExtractionCall[Noise](
            result=result,
            raw_response=raw_response,
            raw_relevance=parsed.relevance,
            prompt_version=self.SPEC.version,
            model_name=self.SPEC.model,
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
