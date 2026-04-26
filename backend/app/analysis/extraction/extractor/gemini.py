"""Gemini 実装の Content Extractor — Stage 1。"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.extraction.domain import ExtractionResult
from app.analysis.extraction.extractor.base import BaseExtractor
from app.config import settings

logger = structlog.get_logger(__name__)

EXTRACTION_PROMPT = """\
あなたはテックニュース記事から事実情報を抽出するアシスタントです。\
入力記事は日本語または英語のいずれかで、出力は常に日本語の構造化データで返します。

記事タイトル: {title}

記事本文:
{content}

以下の 3 項目を抽出してください。

1. title_ja — 記事タイトルの自然な日本語表現。
   - 記事が英語なら正確に和訳する
   - 記事が日本語ならそのまま、または意味を保ったまま整える
   - 過度な意訳・要約はしない

2. summary_ja — 記事内容の事実ベースの日本語要約。
   含めるべき情報:
   - 誰が・どこで・何をしたか（主体と行動）
   - 具体的な数値（金額、規模、日付、バージョン、性能指標など）
   - 技術的新規性（何が新しく、既存手法とどう違うか）
   含めてはいけない情報:
   - あなた自身の判断・評価・推測
   - 業界へのインパクト評価
   - 投資判断や市場予測
   記事に書かれた事実を正確に日本語で再構成してください。

3. entities — 記事中で明示的に言及された固有表現のリスト。
   各エンティティには短い type ラベル（英語）を付与してください。\
例: "company", "product", "technology", "person", "organization", \
"country", "regulation", "vulnerability" など。
   ルール:
   - 記事中で明示されているものだけを抽出する
   - 一般名詞（"AI", "semiconductor" など）は含めない
   - エンティティ名 (name) は記事に登場する表記のまま \
（英語なら英語、日本語なら日本語）で残す
"""


class GeminiExtractor(BaseExtractor):
    """BaseExtractor の Gemini API 実装。"""

    MODEL = "gemini-2.5-flash-lite"
    RPM = 100
    RPD = 1500
    CONTENT_MAX_LENGTH = 8000

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def extract(
        self,
        title: str,
        content: str,
    ) -> ExtractionResult:
        """プロンプトを構築し API を呼び出して構造化レスポンスを返す。"""
        truncated = content[: self.CONTENT_MAX_LENGTH]

        prompt = EXTRACTION_PROMPT.format(
            title=title,
            content=truncated,
        )

        return await self._call_once(prompt)

    async def _call_api(self, prompt: str) -> ExtractionResult:
        """Gemini の generate_content API を呼び出し構造化出力を受け取る。"""
        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048,
                response_mime_type="application/json",
                response_schema=ExtractionResult,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, ExtractionResult):
            raise ProviderError(
                f"Gemini did not return ExtractionResult (got {type(parsed).__name__})"
            )
        return parsed

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, ValidationError):
            return ProviderError(f"Invalid extraction result schema: {exc}")

        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                return ConfigurationError(f"API key leaked: {message}")

            if status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "FAILED_PRECONDITION",
                "NOT_FOUND",
            ):
                return ConfigurationError(f"{status}: {message}")

            if status in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
                return InvalidInputError(f"{status}: {message}")

            if status == "RESOURCE_EXHAUSTED":
                return RateLimitError(f"{status}: {message}")

            if isinstance(exc, ServerError):
                return ProviderError(f"{status}: {message}")

            return UnclassifiedError(
                f"Unhandled APIError {exc.code} {status}: {message}"
            )

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        return UnclassifiedError(f"{type(exc).__name__}: {exc}")
