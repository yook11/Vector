"""DeepSeek 実装の Assessor — Stage 4。

OpenAI SDK を ``base_url=https://api.deepseek.com/beta`` で再利用し、
Function Calling + ``strict: true`` + inline flat schema で構造化出力を強制する。
PoC で ``$ref``/``$defs`` 経由の制約は AI が enforce しないことを確認済
(specs/stage2-deepseek-migration.md)。

Prompt 文面は ``DeepSeekAssessmentPrompt`` が SSoT、call config (model /
gen_config / response_schema / tool_name / base_url / version / rate_policy) は
``DEEPSEEK_ASSESSMENT_SPEC`` (``spec.py``) が SSoT。本 class は I/O 駆動
(SDK 例外翻訳) に責務を絞る。

PR3 で:
- 戻り値を ``AssessmentResult`` 直接 → ``AssessmentCall`` envelope に切り替え
- ``ClassificationRawResponse.model_validate_json()`` 経由を削除し、
  tool_call.arguments → ``json.loads`` → ``parse_assessment`` の流れに統一
- tool_call 欠落 / wrong tool name / arguments JSON 不正は
  ``AssessmentResponseInvalidError`` (recoverable / cron 救済対象) で raise する。
  ``AIProviderRequestInvalidError`` (terminal-skip) で raise しないのは、モデルの
  一時的な tool 省略を「リトライしても無駄」扱いにしないため。
- ``_translate_error`` を spec の DeepSeek (OpenAI 互換) SDK 翻訳テーブル
  (``AIProvider*Error`` 系への翻訳) に書き直し、catch-all は exc を return する
  bare re-raise guard 規約
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
from app.analysis.rate_policy import RatePolicy
from app.config import settings

logger = structlog.get_logger(__name__)


class DeepSeekAssessor(BaseAssessor):
    """BaseAssessor の DeepSeek-V4-Flash 実装。"""

    SPEC: Final[DeepSeekAssessmentSpec] = DEEPSEEK_ASSESSMENT_SPEC

    def __init__(self) -> None:
        api_key = settings.deepseek_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError("DEEPSEEK_API_KEY is not configured")
        self._client = AsyncOpenAI(api_key=api_key, base_url=self.SPEC.base_url)

    # -- BaseAssessor property 契約 --

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def prompt_version(self) -> str:
        return self.SPEC.version

    @property
    def rate_policy(self) -> RatePolicy:
        return self.SPEC.rate_policy

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
            # provider は応答したが期待した tool_call 構造を返さなかった
            # (= response schema 違反)。AIProviderRequestInvalidError
            # (terminal-skip / retry 不能) ではなく、AssessmentResponseInvalidError
            # (recoverable / cron 救済) で raise する。モデル一時的な tool 省略を
            # 「リトライ無駄」扱いにしないため。
            raise AssessmentResponseInvalidError(
                f"DeepSeek did not return {tool_name} tool_call "
                f"(finish_reason={choice.finish_reason})"
            )

        raw_arguments = tool_calls[0].function.arguments or ""
        try:
            payload = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise AssessmentResponseInvalidError(
                f"DeepSeek tool arguments are not valid JSON: {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise AssessmentResponseInvalidError(
                f"DeepSeek tool arguments are not a JSON object: "
                f"{type(payload).__name__}"
            )

        # parse_assessment を先に通すことで PR2 strict 規約 (3 key 存在 + str 型強制)
        # を担保。通過後の payload["category"] / payload["topic"] は str 確定なので
        # str() 暗黙 coerce を入れない (silent な None / int の文字列化を許さない)。
        result = parse_assessment(payload)
        raw_category = payload["category"]
        raw_topic = payload["topic"]
        # match で result を narrow して container 単位の Generic 型を確定する
        # (``AssessmentCall[InScope]`` / ``AssessmentCall[OutOfScope]``)。
        match result:
            case InScope():
                return AssessmentCall(
                    result=result,
                    raw_response=raw_arguments,
                    raw_category=raw_category,
                    raw_topic=raw_topic,
                    prompt_version=self.SPEC.version,
                    model_name=self.SPEC.model,
                )
            case OutOfScope():
                return AssessmentCall(
                    result=result,
                    raw_response=raw_arguments,
                    raw_category=raw_category,
                    raw_topic=raw_topic,
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
        # network 系
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")

        if isinstance(exc, (AuthenticationError, PermissionDeniedError, NotFoundError)):
            return AIProviderConfigurationError(str(exc))

        # HTTP 402 を OpenAIRateLimitError より先に評価 (DeepSeek 固有)
        if isinstance(exc, APIStatusError) and exc.status_code == 402:
            return AIProviderInsufficientBalanceError(
                f"DeepSeek insufficient balance (HTTP 402): {exc}"
            )

        if isinstance(exc, OpenAIRateLimitError):
            return AIProviderRateLimitedError(f"DeepSeek rate limit: {exc}")

        if isinstance(exc, (BadRequestError, UnprocessableEntityError)):
            return AIProviderRequestInvalidError(f"DeepSeek bad request: {exc}")

        if isinstance(exc, InternalServerError):
            return AIProviderServiceUnavailableError(f"DeepSeek server error: {exc}")

        if isinstance(exc, APIStatusError) and 500 <= exc.status_code < 600:
            return AIProviderServiceUnavailableError(
                f"DeepSeek server error (HTTP {exc.status_code}): {exc}"
            )

        return exc  # bare re-raise (UNKNOWN)
