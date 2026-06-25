"""DeepSeek 実装の Assessor — Stage 4。

OpenAI SDK を ``base_url=https://api.deepseek.com/beta`` で再利用し、
Function Calling + ``strict: true`` + inline flat schema で構造化出力を強制する。
``$ref``/``$defs`` 経由の制約は AI が enforce しないため使わない。

Prompt 文面は ``DeepSeekAssessmentPrompt`` が SSoT、call config (model /
gen_config / response_schema / tool_name / base_url / version / rate_limit_policy) は
``DEEPSEEK_ASSESSMENT_SPEC`` (``spec.py``) が SSoT。本 class は I/O 駆動
(SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Final

import structlog
from openai import AsyncOpenAI

from app.analysis.ai_provider_errors import AIProviderConfigurationError
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.deepseek_prompt import DeepSeekAssessmentPrompt
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.parse import parse_assessment
from app.analysis.assessment.ai.spec import (
    DEEPSEEK_ASSESSMENT_SPEC,
    DeepSeekAssessmentSpec,
)
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.analysis.deepseek_error_translator import (
    DeepSeekStateReason,
    translate_deepseek_error,
)
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.config import settings

logger = structlog.get_logger(__name__)


class DeepSeekResponseDefect(StrEnum):
    """DeepSeek adapter が検知する envelope 契約違反 (自己記述コード)。

    spec は ``tool_choice`` で ``assess_article`` の呼び出しを強制している。
    それでも tool_call が欠落 / tool 名が違う / arguments が非 JSON・非 object に
    なるのは provider が機構契約を破った状態で、parse が扱う「内容の schema 違反」
    とは別レイヤ。検知場所である本 adapter が語彙を所有し、value はそのまま audit
    の ``outcome_code`` に焼かれる。
    """

    NO_TOOL_CALL = "assessment_response_deepseek_no_tool_call"
    WRONG_TOOL_NAME = "assessment_response_deepseek_wrong_tool_name"
    ARGUMENTS_NOT_JSON = "assessment_response_deepseek_arguments_not_json"
    ARGUMENTS_NOT_DICT = "assessment_response_deepseek_arguments_not_dict"


class DeepSeekAssessor(BaseAssessor):
    """BaseAssessor の DeepSeek-V4-Flash 実装。"""

    SPEC: Final[DeepSeekAssessmentSpec] = DEEPSEEK_ASSESSMENT_SPEC

    def __init__(self) -> None:
        api_key = settings.deepseek_api_key.get_secret_value()
        if not api_key:
            # provider error detail に secret や provider message を含めない。
            # reason で「未設定」を他の configuration 原因と区別する。
            raise AIProviderConfigurationError(
                reason=DeepSeekStateReason.NOT_CONFIGURED
            )
        self._client = AsyncOpenAI(api_key=api_key, base_url=self.SPEC.base_url)

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
        """Stage 3 (Curation) の出力を判定する。原文は読まない。"""
        prompt = DeepSeekAssessmentPrompt.render(
            title_ja=title_ja, summary_ja=summary_ja
        )
        return await self._call_once(prompt)

    async def _call_api(
        self, prompt: str
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        """DeepSeek の chat.completions API を Function Calling 経由で呼び出す。

        SDK レスポンスは tool_call.arguments を ``json.loads`` →
        ``parse_assessment`` でドメイン型 (``InScope`` / ``OutOfScope``) に
        詰め替え、raw 情報と共に ``AssessmentCall`` envelope に格納する。
        """
        tool_name = self.SPEC.tool_name
        resp = await self._client.chat.completions.create(
            model=self.SPEC.model,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "strict": True,
                        "description": (
                            "記事を Vector の 12 カテゴリのいずれか、"
                            "または out_of_scope に分類する"
                        ),
                        "parameters": dict(self.SPEC.response_schema),
                    },
                }
            ],
            **self.SPEC.gen_config,
            **self.SPEC.structured_output,
        )

        choice = resp.choices[0]
        # truncation 観測信号 (raw arguments = PII は載せない)。finish_reason="length"
        # + completion_tokens≈max_tokens なら出力切れで JSON が壊れたと切り分けられる。
        finish_reason = choice.finish_reason
        completion_tokens = resp.usage.completion_tokens if resp.usage else None
        try:
            tool_calls = choice.message.tool_calls or []
            # tool_call 構造違反は AI 応答の schema 違反として扱い、terminal な request
            # invalid にはしない。tool_call 欠落と tool 名相違を別 defect に分ける。
            if not tool_calls:
                raise AssessmentResponseInvalidError(
                    DeepSeekResponseDefect.NO_TOOL_CALL
                )
            if tool_calls[0].function.name != tool_name:
                raise AssessmentResponseInvalidError(
                    DeepSeekResponseDefect.WRONG_TOOL_NAME
                )

            raw_arguments = tool_calls[0].function.arguments or ""
            try:
                payload = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                # raw AI 応答は例外 message に含めない。
                raise AssessmentResponseInvalidError(
                    DeepSeekResponseDefect.ARGUMENTS_NOT_JSON
                ) from exc

            if not isinstance(payload, dict):
                raise AssessmentResponseInvalidError(
                    DeepSeekResponseDefect.ARGUMENTS_NOT_DICT
                )

            # parse_assessment を先に通すことで strict 規約 (3 key 存在 + str 型強制)
            # を担保。通過後の payload["category"] は str 確定なので str() 暗黙 coerce
            # を入れない (silent な None / int の文字列化を許さない)。
            result = parse_assessment(payload)
        except AssessmentResponseInvalidError as exc:
            # 応答が使えない時に truncation 判定材料を残す (失敗の扱いは変えない)。
            logger.warning(
                "assessment_deepseek_response_defect",
                code=exc.code,
                finish_reason=finish_reason,
                completion_tokens=completion_tokens,
                max_tokens=self.SPEC.gen_config.get("max_tokens"),
            )
            raise

        raw_category = payload["category"]
        # match で result を narrow して container 単位の Generic 型を確定する
        # (``AssessmentCall[InScope]`` / ``AssessmentCall[OutOfScope]``)。
        match result:
            case InScope():
                return AssessmentCall(
                    result=result,
                    raw_response=raw_arguments,
                    raw_category=raw_category,
                    prompt_version=self.SPEC.version,
                    model_name=self.SPEC.model,
                )
            case OutOfScope():
                return AssessmentCall(
                    result=result,
                    raw_response=raw_arguments,
                    raw_category=raw_category,
                    prompt_version=self.SPEC.version,
                    model_name=self.SPEC.model,
                )

    def _translate_error(self, exc: Exception) -> Exception:
        """SDK 例外翻訳は共通 translator に委譲する (Gemini adapter と対称)。"""
        return translate_deepseek_error(exc)
