"""Gemini 実装の Assessor — Stage 4。

Prompt 文面 / model / gen_config / response schema は ``GeminiAssessmentPrompt``
が SSoT。本 class は I/O 駆動 (rate limit + SDK 例外翻訳) に責務を絞る。

PR3 で:
- 戻り値を ``AssessmentResult`` 直接 → ``AssessmentCall`` envelope に切り替え
- ``ClassificationRawResponse`` 経由を削除し、SDK text → ``json.loads`` →
  ``parse_assessment`` の流れに統一 (PR2 で導入済の AI 境界 ACL)
- ``finish_reason == SAFETY|RECITATION`` を ``_call_api`` 内で
  ``AIProviderOutputBlockedError`` に直接 raise (translate 経由ではない)
- ``_translate_error`` を spec の Gemini SDK 翻訳テーブル (``AIProvider*Error``
  系への翻訳) に書き直し、catch-all は exc を return する bare re-raise guard 規約
"""

from __future__ import annotations

import json

import httpx
import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai.types import GenerateContentConfig

from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.gemini_prompt import GeminiAssessmentPrompt
from app.analysis.assessment.ai.parse import parse_assessment
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.analysis.errors.provider import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.config import settings

logger = structlog.get_logger(__name__)

# Gemini が応答を抑制した場合の finish_reason 値。SDK 経由で出力 block を直接
# 知らせるシグナルなので、_translate_error 経由ではなく _call_api 内で
# AIProviderOutputBlockedError に直接 raise する。
_BLOCKED_FINISH_REASONS = frozenset({"SAFETY", "RECITATION"})


class GeminiAssessor(BaseAssessor):
    """BaseAssessor の Gemini API 実装。"""

    MODEL = GeminiAssessmentPrompt.MODEL
    RPM = 100
    RPD = 1500

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

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
            model=GeminiAssessmentPrompt.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                **GeminiAssessmentPrompt.GEN_CONFIG,
                response_schema=dict(GeminiAssessmentPrompt.RESPONSE_SCHEMA),
            ),
        )

        # finish_reason は出力 block の直接シグナル。translate_error を経由せず
        # _call_api 内で raise する (provider が「応答を抑制した」事実は SDK の
        # 例外ではなくレスポンス attribute として届く)。
        finish_reason_name = self._extract_finish_reason_name(response)
        if finish_reason_name in _BLOCKED_FINISH_REASONS:
            raise AIProviderOutputBlockedError(
                f"gemini blocked output: finish_reason={finish_reason_name}"
            )

        text = response.text or ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssessmentResponseInvalidError(
                f"Gemini response is not valid JSON: {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise AssessmentResponseInvalidError(
                f"Gemini response is not a JSON object: {type(payload).__name__}"
            )

        # parse_assessment を先に通すことで PR2 strict 規約 (3 key 存在 + str 型強制)
        # を担保。通過後の payload["category"] / payload["topic"] は str 確定なので
        # str() 暗黙 coerce を入れない (silent な None / int の文字列化を許さない)。
        # 失敗時は AssessmentResponseInvalidError が伝播し envelope 構築は飛ぶ。
        result = parse_assessment(payload)
        raw_category = payload["category"]
        raw_topic = payload["topic"]
        # match で result を narrow して container 単位の Generic 型を確定する
        # (``AssessmentCall[InScope]`` / ``AssessmentCall[OutOfScope]``)。
        match result:
            case InScope():
                return AssessmentCall(
                    result=result,
                    raw_response=text,
                    raw_category=raw_category,
                    raw_topic=raw_topic,
                    prompt_version=GeminiAssessmentPrompt.VERSION,
                    model_name=self.MODEL,
                )
            case OutOfScope():
                return AssessmentCall(
                    result=result,
                    raw_response=text,
                    raw_category=raw_category,
                    raw_topic=raw_topic,
                    prompt_version=GeminiAssessmentPrompt.VERSION,
                    model_name=self.MODEL,
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
        """Gemini SDK / httpx 例外を ``AIProvider*Error`` 階層に翻訳する。

        spec §Gemini SDK 翻訳テーブルに 1:1 対応。マップできなければ ``exc`` を
        そのまま return (caller である ``_call_once`` が bare re-raise する規約)。

        google-genai 1.x の ``ClientError`` は ``code`` (int HTTP status) と
        ``status`` (gRPC status 文字列、e.g. "INVALID_ARGUMENT") の両方を
        attribute として持つので、両経路を見て robust に判定する。
        """
        # network 系 (httpx は SDK の transport)
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")

        # genai SDK の例外階層 (HTTP status + gRPC status の両方を見る)
        if isinstance(exc, genai_errors.ClientError):
            code = getattr(exc, "code", None)
            status = getattr(exc, "status", None) or ""
            raw_message = str(getattr(exc, "message", "")) or str(exc)
            message = raw_message.lower()

            # red-team chain γ-1: SDK 生 message に key prefix /
            # Authorization header が含まれる経路があるため固定文言に丸める。
            # 詳細 debug は error_chain (SDK class FQN) で代替。
            if "reported as leaked" in message:
                return AIProviderConfigurationError(
                    "Gemini API key has been reported as leaked; rotate immediately"
                )

            if code == 400 or status == "INVALID_ARGUMENT":
                if "api key" in message or "permission" in message:
                    return AIProviderConfigurationError(str(exc))
                if "blocked" in message or "safety" in message:
                    return AIProviderInputRejectedError(str(exc))
                return AIProviderRequestInvalidError(str(exc))
            if code in (401, 403, 404) or status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "NOT_FOUND",
                "FAILED_PRECONDITION",
            ):
                return AIProviderConfigurationError(str(exc))
            if code == 429 or status == "RESOURCE_EXHAUSTED":
                if "quota" in message or "daily" in message:
                    return AIProviderQuotaExhaustedError(str(exc))
                return AIProviderRateLimitedError(str(exc))

        if isinstance(exc, genai_errors.ServerError):
            return AIProviderServiceUnavailableError(str(exc))

        return exc  # bare re-raise (UNKNOWN)
