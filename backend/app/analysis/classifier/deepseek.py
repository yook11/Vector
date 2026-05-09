"""DeepSeek 実装の Classifier — Stage 4。

OpenAI SDK を ``base_url=https://api.deepseek.com/beta`` で再利用し、
Function Calling + ``strict: true`` + inline flat schema で構造化出力を強制する。
PoC で ``$ref``/``$defs`` 経由の制約は AI が enforce しないことを確認済
(specs/stage2-deepseek-migration.md)。

Prompt 文面 / model / gen_config / tool schema は ``DeepSeekClassificationPrompt``
が SSoT。本 class は I/O 駆動 (SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

from typing import Final

import structlog
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import ValidationError

from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.deepseek_prompt import DeepSeekClassificationPrompt
from app.analysis.classifier.prompts import to_domain
from app.analysis.classifier.schema import (
    AssessmentResult,
    ClassificationRawResponse,
)
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InsufficientBalanceError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.config import settings

logger = structlog.get_logger(__name__)

# DeepSeek beta endpoint (client constructor 用、Prompt 概念ではない)
_BASE_URL: Final = "https://api.deepseek.com/beta"


class DeepSeekClassifier(BaseClassifier):
    """BaseClassifier の DeepSeek-V4-Flash 実装。"""

    MODEL = DeepSeekClassificationPrompt.MODEL
    # 公式の固定 RPM/RPD 公開なし。429 は OpenAI SDK の retry に任せ、
    # Logfire 実測後に値を入れる方針 (別 PR)。
    RPM: int | None = None
    RPD: int | None = None

    def __init__(self) -> None:
        api_key = settings.deepseek_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("DEEPSEEK_API_KEY is not configured")
        self._client = AsyncOpenAI(api_key=api_key, base_url=_BASE_URL)

    async def classify(
        self,
        title_ja: str,
        summary_ja: str,
    ) -> AssessmentResult:
        """Stage 3 (Extraction) の出力を判定する。原文は読まない。"""
        prompt = DeepSeekClassificationPrompt.render(
            title_ja=title_ja, summary_ja=summary_ja
        )
        return await self._call_once(prompt)

    async def _call_api(self, prompt: str) -> AssessmentResult:
        """DeepSeek の chat.completions API を Function Calling 経由で呼び出す。"""
        tool_name = DeepSeekClassificationPrompt.TOOL_NAME
        resp = await self._client.chat.completions.create(
            model=DeepSeekClassificationPrompt.MODEL,
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
                        "parameters": dict(
                            DeepSeekClassificationPrompt.RESPONSE_SCHEMA
                        ),
                    },
                }
            ],
            **DeepSeekClassificationPrompt.GEN_CONFIG,
        )

        choice = resp.choices[0]
        tool_calls = choice.message.tool_calls or []
        if not tool_calls or tool_calls[0].function.name != tool_name:
            raise ProviderError(
                f"DeepSeek did not return {tool_name} tool_call "
                f"(finish_reason={choice.finish_reason})"
            )

        # AI 境界 schema は subset 外制約 (minLength/maxLength 等) を含まないため、
        # ここで Pydantic 再検証を行う (PoC で確定した 2 段構成)。
        raw = ClassificationRawResponse.model_validate_json(
            tool_calls[0].function.arguments
        )
        return to_domain(raw)

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """OpenAI SDK の例外を Vector のエラー階層に分類する。"""
        if isinstance(exc, ValidationError):
            return ProviderError(f"Invalid classification response schema: {exc}")

        if isinstance(exc, AuthenticationError):
            return ConfigurationError(f"DeepSeek auth failed: {exc}")

        if isinstance(exc, PermissionDeniedError):
            return ConfigurationError(f"DeepSeek permission denied: {exc}")

        # HTTP 402 (Insufficient Balance) は専用 SDK 例外がないので
        # APIStatusError.status_code で判定する。OpenAIRateLimitError 等の
        # 専用サブクラスより先に評価する。
        if isinstance(exc, APIStatusError) and exc.status_code == 402:
            return InsufficientBalanceError(
                f"DeepSeek insufficient balance (HTTP 402): {exc}"
            )

        if isinstance(exc, OpenAIRateLimitError):
            return RateLimitError(f"DeepSeek rate limit (HTTP 429): {exc}")

        if isinstance(exc, (BadRequestError, UnprocessableEntityError)):
            return InvalidInputError(f"DeepSeek bad request: {exc}")

        if isinstance(exc, APIStatusError) and 500 <= exc.status_code < 600:
            return ProviderError(
                f"DeepSeek server error (HTTP {exc.status_code}): {exc}"
            )

        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        return UnclassifiedError(f"{type(exc).__name__}: {exc}")
