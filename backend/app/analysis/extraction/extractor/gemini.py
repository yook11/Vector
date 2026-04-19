"""Gemini 実装の Content Extractor — Stage 1。"""

from __future__ import annotations

import json

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig

from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.extraction.extractor.base import (
    BaseExtractor,
    EntityData,
    ExtractionData,
)
from app.config import settings
from app.models.article_entity import EntityType

logger = structlog.get_logger(__name__)

_VALID_ENTITY_TYPES = frozenset(t.value for t in EntityType)

EXTRACTION_PROMPT = """\
You are a tech news content extractor. Your job is to extract factual \
information from English tech news articles and output structured data \
in Japanese.

You must respond ONLY with a valid JSON object. Do not include markdown \
code fences or any text outside the JSON.

Article title: {title}

Article full text:
{content}

Extract the following:

1. title_ja — Accurate Japanese translation of the article title.

2. summary_ja — A factual Japanese summary of the article.
   Include:
   - Who did what, where (subjects and actions)
   - Specific numbers: amounts, scale, dates, version numbers, performance metrics
   - Technical novelty: what is new, how it differs from existing approaches
   Do NOT include:
   - Your own judgment, evaluation, or speculation
   - Industry impact assessment
   - Investment implications or market predictions
   Reconstruct the facts written in the article accurately in Japanese.

3. entities — A structured list of named entities mentioned in the article.
   Entity types:
   - "company": Companies and organizations (e.g. Anthropic, TSMC, NASA)
   - "product": Products and services (e.g. Claude 4, GPT-5, Falcon 9)
   - "technology": Technologies and frameworks \
(e.g. constitutional AI, EUV lithography, CRISPR)
   Rules:
   - Only extract entities explicitly mentioned in the article
   - Do NOT include generic terms ("AI", "semiconductor", etc.)
   - Remove duplicates

Return a JSON object:
{{
  "title_ja": "Japanese title",
  "summary_ja": "Factual Japanese summary",
  "entities": [
    {{"name": "EntityName", "type": "company|product|technology"}}
  ]
}}
"""


class GeminiExtractor(BaseExtractor):
    """BaseExtractor の Gemini API 実装。"""

    MODEL = "gemini-2.5-flash-lite"
    RPM = 50
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
    ) -> ExtractionData:
        """プロンプトを構築し API を呼び出して抽出結果を解析する。"""
        truncated = content[: self.CONTENT_MAX_LENGTH]

        prompt = EXTRACTION_PROMPT.format(
            title=title,
            content=truncated,
        )

        raw_text = await self._call_once(prompt)
        return self._parse_response(raw_text)

    async def _call_api(self, prompt: str) -> str:
        """Gemini の generate_content API を呼び出す。"""
        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048,
            ),
        )
        if response.text is None:
            raise ProviderError("Gemini returned empty response")
        return response.text

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
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

    def _parse_response(self, raw_text: str) -> ExtractionData:
        """Gemini からの JSON レスポンスを解析・検証する。"""
        text = raw_text.strip()

        if text.startswith("```"):
            first_newline = text.index("\n")
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(
                "extractor_json_parse_error",
                raw_text=raw_text[:500],
                error=str(e),
            )
            raise ProviderError(f"Failed to parse Gemini response as JSON: {e}")

        try:
            title_ja = str(data["title_ja"]).strip()
            summary_ja = str(data["summary_ja"]).strip()

            if not title_ja:
                raise ProviderError("title_ja is empty")
            if not summary_ja:
                raise ProviderError("summary_ja is empty")

            # エンティティのパースと検証
            raw_entities = data.get("entities", [])
            seen: set[tuple[str, str]] = set()
            entities: list[EntityData] = []

            for raw in raw_entities:
                name = str(raw["name"]).strip()
                entity_type = str(raw["type"]).strip().lower()

                if entity_type not in _VALID_ENTITY_TYPES:
                    logger.warning(
                        "extractor_invalid_entity_type",
                        name=name,
                        type=entity_type,
                    )
                    continue

                key = (name.lower(), entity_type)
                if key in seen:
                    continue
                seen.add(key)

                entities.append(EntityData(name=name, type=EntityType(entity_type)))

            return ExtractionData(
                title_ja=title_ja,
                summary_ja=summary_ja,
                entities=entities,
            )
        except (KeyError, TypeError) as e:
            logger.error(
                "extractor_validation_error",
                data=data,
                error=str(e),
            )
            raise ProviderError(f"Invalid extraction data from Gemini: {e}")
