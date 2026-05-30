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
from typing import Final

import structlog
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from openai import RateLimitError as OpenAIRateLimitError

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
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
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.config import settings

logger = structlog.get_logger(__name__)


class DeepSeekAssessor(BaseAssessor):
    """BaseAssessor の DeepSeek-V4-Flash 実装。"""

    SPEC: Final[DeepSeekAssessmentSpec] = DEEPSEEK_ASSESSMENT_SPEC

    def __init__(self) -> None:
        api_key = settings.deepseek_api_key.get_secret_value()
        if not api_key:
            # provider error detail に secret や provider message を含めない。
            raise AIProviderConfigurationError()
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
        """Stage 3 (Extraction) の出力を判定する。原文は読まない。"""
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
                            "記事を Vector の 11 カテゴリのいずれか、"
                            "または out_of_scope に分類する"
                        ),
                        "parameters": dict(self.SPEC.response_schema),
                    },
                }
            ],
            **self.SPEC.gen_config,
        )

        choice = resp.choices[0]
        tool_calls = choice.message.tool_calls or []
        if not tool_calls or tool_calls[0].function.name != tool_name:
            # tool_call 構造欠落は AI 応答の schema 違反として扱い、
            # terminal な request invalid にはしない。
            raise AssessmentResponseInvalidError()

        raw_arguments = tool_calls[0].function.arguments or ""
        try:
            payload = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            # raw AI 応答は例外 message に含めない。
            raise AssessmentResponseInvalidError() from exc

        if not isinstance(payload, dict):
            raise AssessmentResponseInvalidError()

        # parse_assessment を先に通すことで strict 規約 (3 key 存在 + str 型強制)
        # を担保。通過後の payload["category"] は str 確定なので str() 暗黙 coerce
        # を入れない (silent な None / int の文字列化を許さない)。
        result = parse_assessment(payload)
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
        """OpenAI SDK 例外を ``AIProvider*Error`` 階層に翻訳する。

        spec §DeepSeek SDK 翻訳テーブルに 1:1 対応。HTTP 402 (Insufficient
        Balance) は専用 SDK 例外がないので ``APIStatusError.status_code`` で判定し、
        ``OpenAIRateLimitError`` 等の専用サブクラスより先に評価する。

        マップできなければ ``exc`` をそのまま return (caller である
        ``_call_once`` が bare re-raise する規約)。
        """
        # network 系。SDK 生 message は provider error detail に載せない。
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return AIProviderNetworkError()
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return AIProviderNetworkError()

        if isinstance(exc, (AuthenticationError, PermissionDeniedError, NotFoundError)):
            return AIProviderConfigurationError()

        # HTTP 402 を OpenAIRateLimitError より先に評価 (DeepSeek 固有)
        if isinstance(exc, APIStatusError) and exc.status_code == 402:
            return AIProviderInsufficientBalanceError()

        if isinstance(exc, OpenAIRateLimitError):
            return AIProviderRateLimitedError()

        if isinstance(exc, (BadRequestError, UnprocessableEntityError)):
            return AIProviderRequestInvalidError()

        if isinstance(exc, InternalServerError):
            return AIProviderServiceUnavailableError()

        if isinstance(exc, APIStatusError) and 500 <= exc.status_code < 600:
            return AIProviderServiceUnavailableError()

        return exc  # bare re-raise (UNKNOWN)
