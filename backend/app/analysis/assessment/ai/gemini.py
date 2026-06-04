"""Gemini 実装の Assessor — Stage 4。

Prompt 文面は ``GeminiAssessmentPrompt`` が SSoT、call config (model /
gen_config / response_schema / version / rate_limit_policy) は
``GEMINI_ASSESSMENT_SPEC`` (``spec.py``) が SSoT。本 class は I/O 駆動
(rate limit + SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Final

import structlog
from google import genai
from google.genai.types import GenerateContentConfig

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderOutputBlockedError,
)
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.gemini_prompt import GeminiAssessmentPrompt
from app.analysis.assessment.ai.parse import parse_assessment
from app.analysis.assessment.ai.spec import (
    GEMINI_ASSESSMENT_SPEC,
    AssessmentCallSpec,
)
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.analysis.gemini_error_translator import translate_gemini_error
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.config import settings

logger = structlog.get_logger(__name__)

# Gemini が応答を抑制した場合の finish_reason 値。SDK 経由で出力 block を直接
# 知らせるシグナルなので、_translate_error 経由ではなく _call_api 内で
# AIProviderOutputBlockedError に直接 raise する。
_BLOCKED_FINISH_REASONS = frozenset({"SAFETY", "RECITATION"})


class GeminiResponseDefect(StrEnum):
    """Gemini adapter が検知する envelope 契約違反 (自己記述コード)。

    spec は ``response_mime_type=application/json`` で JSON 出力を強制している。
    それでも非 JSON / 非 object が返るのは provider が機構契約を破った状態で、
    parse が扱う「内容の schema 違反」とは別レイヤ。検知場所である本 adapter が
    語彙を所有し、value はそのまま audit の ``outcome_code`` に焼かれる。
    """

    NOT_JSON = "assessment_response_gemini_not_json"
    NOT_OBJECT = "assessment_response_gemini_not_object"


class GeminiAssessor(BaseAssessor):
    """BaseAssessor の Gemini API 実装。"""

    SPEC: Final[AssessmentCallSpec] = GEMINI_ASSESSMENT_SPEC

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            # provider error detail に secret や provider message を含めない。
            raise AIProviderConfigurationError()
        self._client = genai.Client(api_key=api_key)

    # -- BaseAssessor property 契約 --

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def prompt_version(self) -> str:
        return self.SPEC.version

    @property
    def rate_limit_policy(self) -> AIModelRateLimitPolicy:
        return self.SPEC.rate_limit_policy

    async def assess(
        self,
        title_ja: str,
        summary_ja: str,
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        """Stage 3 (Extraction) の出力を判定する。原文は読まない。"""
        prompt = GeminiAssessmentPrompt.render(title_ja=title_ja, summary_ja=summary_ja)
        return await self._call_once(prompt)

    async def _call_api(
        self, prompt: str
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        """Gemini の generate_content API を呼び出し ``AssessmentCall`` を返す。

        SDK レスポンスは text を ``json.loads`` → ``parse_assessment`` で
        ドメイン型 (``InScope`` / ``OutOfScope``) に詰め替え、raw 情報と共に
        ``AssessmentCall`` envelope に格納する。
        """
        response = await self._client.aio.models.generate_content(
            model=self.SPEC.model,
            contents=prompt,
            config=GenerateContentConfig(
                **self.SPEC.gen_config,
                response_schema=dict(self.SPEC.response_schema),
            ),
        )

        # finish_reason は出力 block の直接シグナル。translate_error を経由せず
        # _call_api 内で raise する (provider が「応答を抑制した」事実は SDK の
        # 例外ではなくレスポンス attribute として届く)。
        finish_reason_name = self._extract_finish_reason_name(response)
        if finish_reason_name in _BLOCKED_FINISH_REASONS:
            raise AIProviderOutputBlockedError()

        text = response.text or ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            # raw AI 応答は例外 message に含めない。
            raise AssessmentResponseInvalidError(GeminiResponseDefect.NOT_JSON) from exc

        if not isinstance(payload, dict):
            raise AssessmentResponseInvalidError(GeminiResponseDefect.NOT_OBJECT)

        # parse_assessment を先に通すことで strict 規約 (3 key 存在 + str 型強制)
        # を担保。通過後の payload["category"] は str 確定なので str() 暗黙 coerce
        # を入れない (silent な None / int の文字列化を許さない)。
        # 失敗時は AssessmentResponseInvalidError が伝播し envelope 構築は飛ぶ。
        result = parse_assessment(payload)
        raw_category = payload["category"]
        # match で result を narrow して container 単位の Generic 型を確定する
        # (``AssessmentCall[InScope]`` / ``AssessmentCall[OutOfScope]``)。
        match result:
            case InScope():
                return AssessmentCall(
                    result=result,
                    raw_response=text,
                    raw_category=raw_category,
                    prompt_version=self.SPEC.version,
                    model_name=self.SPEC.model,
                )
            case OutOfScope():
                return AssessmentCall(
                    result=result,
                    raw_response=text,
                    raw_category=raw_category,
                    prompt_version=self.SPEC.version,
                    model_name=self.SPEC.model,
                )

    @staticmethod
    def _extract_finish_reason_name(response: object) -> str | None:
        """SDK の Response から finish_reason の name を best-effort で抽出する。

        google-genai の Response 構造は version で揺れがあるため getattr 多段で
        防御する (None / 空 candidates / Enum vs string の違いを吸収)。
        """
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        finish_reason = getattr(candidates[0], "finish_reason", None)
        if finish_reason is None:
            return None
        # Enum (FinishReason.SAFETY 等) なら .name、文字列ならそのまま
        return getattr(finish_reason, "name", None) or str(finish_reason)

    def _translate_error(self, exc: Exception) -> Exception:
        """SDK 例外を ``AIProvider*Error`` へ翻訳する (共通 translator に委譲)。"""
        return translate_gemini_error(exc)
