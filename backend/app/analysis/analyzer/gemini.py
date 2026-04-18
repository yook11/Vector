"""Gemini AI analyzer — Google GenAI SDK を用いた具象実装。"""

from __future__ import annotations

import json

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig

from app.analysis.analyzer.base import AnalysisData, BaseAnalyzer
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.config import settings
from app.domain.topic import normalize_topic_name
from app.models.article_analysis import ImpactLevel

logger = structlog.get_logger(__name__)

VALID_CATEGORIES = frozenset(
    [
        "ai_ml",
        "biotech",
        "energy",
        "fintech",
        "materials",
        "quantum",
        "robotics",
        "semiconductor",
        "space",
        "telecom",
    ]
)

ANALYSIS_PROMPT_BASE = """\
You are an expert tech news analyst specializing in emerging technologies \
with a focus on investment implications.

Analyze the following English tech news article and respond ONLY with \
a valid JSON object. Do not include markdown code fences or any text \
outside the JSON.

Article title: {title}
Article description: {description}
{content_section}
Classify this article following these steps:

Step 1 — Determine the category.
Select the single most relevant category from:
- ai_ml: Artificial intelligence and machine learning
- biotech: Biotechnology, pharmaceuticals, genomics
- energy: Energy generation, storage, and sustainability
- fintech: Financial technology, digital payments, blockchain
- materials: Materials science, advanced materials, nanomaterials
- quantum: Quantum computing, quantum sensing, quantum networking
- robotics: Robotics, autonomous vehicles, industrial automation
- semiconductor: Chip design, manufacturing, lithography, and policy
- space: Space launch, satellites, lunar exploration
- telecom: Telecommunications, 5G/6G, network infrastructure

Step 2 — Determine the topic.
Given the category, assign a concise topic label that captures what \
this article is specifically about. Rules:
- Lowercase English, 2-4 words, no articles (a/an/the)
- Use established terminology within the category
- Be specific: prefer "euv lithography advancement" over "semiconductor news"
{existing_topics_section}
Return a JSON object with fields in this exact order:
{{
  "category": "one of the category slugs above",
  "topic": "concise topic label, 2-4 words, lowercase English",
  "title_ja": "Japanese translation of the article title (accurate, concise)",
  "summary_ja": "3-line summary in Japanese. Line 1: key facts. \
Line 2: industry impact. Line 3: investment implications. \
Separate lines with \\n",
  "impact_level": "one of: low, medium, high, critical — how much this \
news affects the market",
  "reasoning": "Brief explanation in Japanese of why you assigned \
this impact level"
}}

Rules:
- All Japanese text must be natural, professional Japanese
- impact_level MUST be exactly one of: "low", "medium", "high", "critical"
- If description is empty, analyze based on the title alone
- When full article content is provided, use it for deeper analysis
"""


def _build_existing_topics_section(
    topics_by_category: dict[str, list[str]] | None,
) -> str:
    """カテゴリ内の既存 Topic リスト（上位30件）をプロンプトに挿入する。"""
    if not topics_by_category:
        return ""

    lines = [
        "Existing topics by category (use these if applicable, "
        "create a new one only if none fit):"
    ]
    for cat_slug, topics in topics_by_category.items():
        topic_list = ", ".join(f'"{t}"' for t in topics[:30])
        lines.append(f"- {cat_slug}: [{topic_list}]")

    return "\n".join(lines) + "\n"


class GeminiAnalyzer(BaseAnalyzer):
    """BaseAnalyzer の Gemini API 実装。"""

    MODEL = "gemini-2.5-flash-lite"
    RPM = 50
    RPD = 1500

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def analyze(
        self,
        title: str,
        description: str | None,
        content: str | None = None,
        existing_topics_by_category: dict[str, list[str]] | None = None,
    ) -> AnalysisData:
        """プロンプトを構築し API を呼び出してレスポンスを解析する。"""
        content_section = ""
        if content:
            truncated = content[: settings.content_max_length]
            content_section = f"\nArticle full text:\n{truncated}\n"

        existing_topics_section = _build_existing_topics_section(
            existing_topics_by_category,
        )

        prompt = ANALYSIS_PROMPT_BASE.format(
            title=title,
            description=description or "(no description available)",
            content_section=content_section,
            existing_topics_section=existing_topics_section,
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

    def _parse_response(self, raw_text: str) -> AnalysisData:
        """Gemini からの JSON レスポンスを解析・検証する。"""
        text = raw_text.strip()

        # Markdown のコードフェンスがあれば除去
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
                "gemini_json_parse_error",
                raw_text=raw_text[:500],
                error=str(e),
            )
            raise ProviderError(f"Failed to parse Gemini response as JSON: {e}")

        try:
            # category バリデーション
            category = str(data["category"]).strip().lower()
            if category not in VALID_CATEGORIES:
                raise ProviderError(
                    f"Invalid category from Gemini: {category!r}. "
                    f"Expected one of: {sorted(VALID_CATEGORIES)}"
                )

            # topic 正規化
            raw_topic = str(data["topic"])
            topic_name = normalize_topic_name(raw_topic)

            impact_level = ImpactLevel(data["impact_level"])

            return AnalysisData(
                title=str(data["title_ja"]),
                summary=str(data["summary_ja"]),
                impact_level=impact_level,
                reasoning=str(data.get("reasoning", "")),
                category_slug=category,
                topic_name=topic_name,
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.error(
                "gemini_validation_error",
                data=data,
                error=str(e),
            )
            raise ProviderError(f"Invalid analysis data from Gemini: {e}")
