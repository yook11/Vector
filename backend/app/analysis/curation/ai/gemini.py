"""Gemini 実装の Content Curator — Stage 3。

Prompt 文面は ``GeminiCurationPrompt``、API call spec (model / gen_config /
response_schema / version / rate policy) は ``GeminiCurationSpec`` singleton
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
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.ai.gemini_prompt import GeminiCurationPrompt
from app.analysis.curation.ai.gemini_spec import (
    GEMINI_CURATION_SPEC,
    GeminiCurationSpec,
)
from app.analysis.curation.ai.parse import parse_curation
from app.analysis.curation.ai.schema import GeminiCurationResponse
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.errors import CurationResponseInvalidError
from app.analysis.gemini_error_translator import (
    is_context_length_error,
    translate_gemini_error,
)
from app.analysis.rate_limit import RatePolicy
from app.config import settings

logger = structlog.get_logger(__name__)

# Gemini が応答を返さなかった理由のうち、**入力内容そのもの** がプロバイダー
# ポリシーに抵触したケース。再試行 / 別モデルでも通らないため記事 DELETE 対象
# (AIProviderOutputBlockedError → Stage 3 boundary で CurationTerminalDropError
# に詰め替えられる)。
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


class GeminiCurator(BaseCurator):
    """BaseCurator の Gemini API 実装。"""

    SPEC: Final[GeminiCurationSpec] = GEMINI_CURATION_SPEC

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    # -- BaseCurator property 契約 --

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def prompt_version(self) -> str:
        return self.SPEC.version

    @property
    def rate_policy(self) -> RatePolicy:
        return self.SPEC.rate_policy

    async def curate(
        self,
        title: str,
        content: str,
    ) -> CurationCall[Signal] | CurationCall[Noise]:
        """プロンプトを構築し API を呼び出して envelope を返す。"""
        prompt = GeminiCurationPrompt.render(title=title, content=content)
        return await self._call_once(prompt)

    async def _call_api(
        self, prompt: str
    ) -> CurationCall[Signal] | CurationCall[Noise]:
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
        # (Stage 3 boundary で CurationTerminalDropError に詰め替えられ、記事
        # DELETE 対象になる)
        finish_reason = _detect_finish_reason(response)
        if finish_reason in _POLICY_BLOCKED_FINISH_REASONS:
            raise AIProviderOutputBlockedError(f"blocked by policy: {finish_reason}")

        parsed = response.parsed
        if not isinstance(parsed, GeminiCurationResponse):
            # provider は応答したが Stage 3 schema として消化不可 (Layer 2-B、
            # CurationRecoverableError 派生、taskiq retry → cron 救済)
            raise CurationResponseInvalidError(
                f"Gemini did not return GeminiCurationResponse "
                f"(got {type(parsed).__name__}, finish_reason={finish_reason})"
            )
        result = parse_curation(parsed)
        # ``CurationCall[T]`` の T は invariant のため Signal | Noise を直接
        # 渡すと ``CurationCall[Signal | Noise]`` に推論される。戻り値型は
        # ``CurationCall[Signal] | CurationCall[Noise]`` なので isinstance で
        # narrow してから明示的に型パラメータを指定する。
        raw_response = _extract_raw_text(response)
        if isinstance(result, Signal):
            return CurationCall[Signal](
                result=result,
                raw_response=raw_response,
                raw_relevance=parsed.relevance,
                prompt_version=self.SPEC.version,
                model_name=self.SPEC.model,
            )
        return CurationCall[Noise](
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
            return CurationResponseInvalidError(
                f"Invalid curation result schema: {exc}"
            )
        if is_context_length_error(exc):
            msg = getattr(exc, "message", None) or str(exc)
            return AIProviderInputRejectedError(f"input exceeds context length: {msg}")
        return translate_gemini_error(exc)
