"""Gemini 実装の Assessor — Stage 4。

Prompt 文面は ``AssessmentPrompt``、call config (model / gen_config /
response_schema / version / provider) は ``GEMINI_ASSESSMENT_SPEC`` (``spec.py``)
が SSoT。本 class は SDK 呼び出しと応答の分類に責務を絞る。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Final

from google.genai.client import AsyncClient
from google.genai.types import (
    FinishReason,
    GenerateContentConfig,
    GenerateContentResponse,
)
from structlog.typing import FilteringBoundLogger

from app.ai_providers.errors import (
    AIProviderResultError,
    AIProviderResultReason,
)
from app.ai_providers.gemini.error_translator import (
    OUTPUT_BLOCKED_FINISH_REASONS,
    output_blocked_reason,
    translate_gemini_error,
)
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.parse import parse_assessment
from app.analysis.assessment.ai.prompts import AssessmentPrompt
from app.analysis.assessment.ai.spec import (
    GEMINI_ASSESSMENT_SPEC,
    GeminiAssessmentSpec,
)
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import AssessmentResponseInvalidError

_KNOWN_FINISH_REASONS: Final[frozenset[str]] = frozenset(
    member.name for member in FinishReason
)


class GeminiResponseDefect(StrEnum):
    """Gemini adapter が検知する応答本文の契約違反 (自己記述コード)。

    JSON として読めない・object でない応答は parse に渡す前の違反で、parse が扱う
    「内容の schema 違反」とは別レイヤ。検知場所である本 adapter が語彙を所有し、
    value はそのまま audit の ``outcome_code`` に焼かれる。
    """

    NOT_JSON = "assessment_response_gemini_not_json"
    NOT_OBJECT = "assessment_response_gemini_not_object"


def _finish_reason_name(response: GenerateContentResponse) -> str | None:
    """先頭 candidate の ``finish_reason`` を名前で返す。"""
    candidates = response.candidates or []
    if not candidates:
        return None
    finish = candidates[0].finish_reason
    if finish is None:
        return None
    name = getattr(finish, "name", None)
    return name if isinstance(name, str) else str(finish)


def _usage_fields(
    response: GenerateContentResponse, *, max_output_tokens: int
) -> dict[str, int]:
    """出力上限と比べられるよう、thinking を含む出力トークン数を返す。"""
    fields: dict[str, int] = {"max_output_tokens": max_output_tokens}
    usage = response.usage_metadata
    candidates = getattr(usage, "candidates_token_count", None)
    if type(candidates) is int and candidates >= 0:
        thoughts = getattr(usage, "thoughts_token_count", None)
        fields["output_tokens"] = candidates + (
            thoughts if type(thoughts) is int and thoughts >= 0 else 0
        )
    return fields


class GeminiAssessor(BaseAssessor):
    """BaseAssessor の Gemini API 実装。"""

    SPEC: Final[GeminiAssessmentSpec] = GEMINI_ASSESSMENT_SPEC

    def __init__(self, *, client: AsyncClient) -> None:
        self._client = client

    # -- BaseAssessor property 契約 --

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def prompt_version(self) -> str:
        return self.SPEC.version

    @property
    def provider(self) -> str:
        return self.SPEC.provider

    async def assess(
        self,
        title_ja: str,
        summary_ja: str,
        *,
        logger: FilteringBoundLogger,
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        """Stage 3 (Curation) の出力を判定する。原文は読まない。"""
        prompt = AssessmentPrompt.render(title_ja=title_ja, summary_ja=summary_ja)
        return await self._call_once(prompt, logger=logger)

    async def _call_api(
        self, prompt: str, *, logger: FilteringBoundLogger
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        """Gemini の generate_content API を呼び出し、応答を判定結果に詰め替える。"""
        response = await self._client.models.generate_content(
            model=self.SPEC.model,
            contents=prompt,
            config=GenerateContentConfig(
                **self.SPEC.gen_config,
                response_schema=dict(self.SPEC.response_schema),
            ),
        )

        prompt_feedback = response.prompt_feedback
        if prompt_feedback is not None and prompt_feedback.block_reason is not None:
            raise AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED)
        finish_reason = _finish_reason_name(response)
        if finish_reason in OUTPUT_BLOCKED_FINISH_REASONS:
            raise AIProviderResultError(reason=output_blocked_reason(finish_reason))
        # 切り詰めは JSON 破損 (NOT_JSON) と別分類にし、容量問題を観測できるようにする。
        if finish_reason == "MAX_TOKENS":
            logger.warning(
                "assessment_gemini_output_truncated",
                reason=AIProviderResultReason.OUTPUT_TRUNCATED.value,
                **_usage_fields(
                    response,
                    max_output_tokens=self.SPEC.gen_config["max_output_tokens"],
                ),
            )
            raise AIProviderResultError(
                "AI応答が出力トークン数の上限に達して打ち切られました",
                reason=AIProviderResultReason.OUTPUT_TRUNCATED,
            )

        raw_response = response.text or ""
        try:
            payload = _decode_payload(raw_response)
            result = parse_assessment(payload)
        except AssessmentResponseInvalidError as exc:
            # 応答が使えない時の観測材料 (raw は載せない。失敗の扱いは変えない)。
            if finish_reason in _KNOWN_FINISH_REASONS:
                logger = logger.bind(finish_reason=finish_reason)
            logger.warning(
                "assessment_gemini_response_defect",
                code=exc.code,
                **_usage_fields(
                    response,
                    max_output_tokens=self.SPEC.gen_config["max_output_tokens"],
                ),
            )
            raise

        # parse_assessment 通過後の category は str 確定。
        raw_category = payload["category"]
        match result:
            case InScope():
                return AssessmentCall(
                    result=result,
                    raw_response=raw_response,
                    raw_category=raw_category,
                    prompt_version=self.SPEC.version,
                    model_name=self.SPEC.model,
                )
            case OutOfScope():
                return AssessmentCall(
                    result=result,
                    raw_response=raw_response,
                    raw_category=raw_category,
                    prompt_version=self.SPEC.version,
                    model_name=self.SPEC.model,
                )

    def _translate_error(self, exc: Exception) -> Exception:
        """SDK 例外翻訳は共通 translator に委譲する。"""
        return translate_gemini_error(exc)


def _decode_payload(raw_response: str) -> dict[str, Any]:
    """応答本文を JSON object として読む。raw AI 応答は例外 message に含めない。"""
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise AssessmentResponseInvalidError(
            GeminiResponseDefect.NOT_JSON,
            message="AI応答をJSONとして解析できません",
        ) from exc
    if not isinstance(payload, dict):
        raise AssessmentResponseInvalidError(
            GeminiResponseDefect.NOT_OBJECT,
            message="AI応答がJSONオブジェクトではありません",
        )
    return payload
