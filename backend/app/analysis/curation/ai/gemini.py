"""Gemini 実装の Content Curator — Stage 3。

Prompt 文面は ``GeminiCurationPrompt``、API call spec (model / gen_config /
response_schema / version / rate policy) は ``GeminiCurationSpec`` singleton
が SSoT。本 class は I/O 駆動 (rate limit + SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

from typing import Final

from google.genai.client import AsyncClient
from google.genai.types import GenerateContentConfig, GenerateContentResponse
from pydantic import ValidationError
from structlog.typing import FilteringBoundLogger

from app.ai_providers.errors import AIProviderResultError
from app.ai_providers.gemini.error_translator import (
    output_blocked_reason,
    translate_gemini_error,
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

# プロバイダーの出力拒否を分類し、処理方針は呼び出し元へ委ねる。
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

    def __init__(self, *, client: AsyncClient) -> None:
        self._client = client

    # -- BaseCurator property 契約 --

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def prompt_version(self) -> str:
        return self.SPEC.version

    @property
    def provider(self) -> str:
        return self.SPEC.provider

    async def curate(
        self,
        title: str,
        content: str,
        *,
        logger: FilteringBoundLogger,
    ) -> CurationCall[Signal] | CurationCall[Noise]:
        """プロンプトを構築し API を呼び出して envelope を返す。"""
        prompt = GeminiCurationPrompt.render(title=title, content=content)
        return await self._call_once(prompt, logger=logger)

    async def _call_api(
        self, prompt: str
    ) -> CurationCall[Signal] | CurationCall[Noise]:
        """Gemini の generate_content API を呼び出し envelope を組み立てる。"""
        response = await self._client.models.generate_content(
            model=self.SPEC.model,
            contents=prompt,
            config=GenerateContentConfig(
                **self.SPEC.gen_config,
                response_schema=self.SPEC.response_schema,
            ),
        )

        finish_reason = _detect_finish_reason(response)
        if (
            finish_reason is not None
            and finish_reason in _POLICY_BLOCKED_FINISH_REASONS
        ):
            # SDK 由来の文字列は出さず、finish_reason は種別ラベルの reason で残す。
            # blocked-set 内なので finish_reason は写像に必ず存在する。
            raise AIProviderResultError(reason=output_blocked_reason(finish_reason))

        parsed = response.parsed
        if not isinstance(parsed, GeminiCurationResponse):
            # provider は応答したが Stage 3 schema として消化不可 (Layer 2-B)。
            # Phase 4: 旧 message 引数廃止。詳細は repository/logger 側で別経路で残す。
            raise CurationResponseInvalidError()
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
        """Pydantic の検証失敗は応答不正とし、SDK 例外は共通の変換器に委ねる。"""
        if isinstance(exc, ValidationError):
            # Phase 4: 旧 message 引数廃止 (PII 含有経路)。Pydantic の詳細は
            # __cause__ 連鎖と structlog 経路で別途残せる。
            return CurationResponseInvalidError()
        return translate_gemini_error(exc)
