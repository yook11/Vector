"""Gemini AI analyzer — concrete implementation using Google GenAI SDK."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog
from google import genai
from google.genai.errors import ClientError
from google.genai.types import GenerateContentConfig

from app.analysis.analyzer.base import AnalysisData, BaseAnalyzer
from app.analysis.errors import (
    AnalysisDomainError,
    InvalidInputError,
    RateLimitError,
    TransientError,
)
from app.config import settings
from app.models.article_analysis import ImpactLevel

if TYPE_CHECKING:
    from app.infra.redis.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)

ANALYSIS_PROMPT_BASE = """\
You are an expert tech news analyst specializing in emerging technologies \
(quantum computing, materials informatics, advanced semiconductors, etc.) \
with a focus on investment implications for the Japanese market.

Analyze the following English tech news article and respond ONLY with \
a valid JSON object. Do not include markdown code fences or any text \
outside the JSON.

Article title: {title}
Article description: {description}
{content_section}
Return a JSON object with exactly these fields:
{{
  "title_ja": "Japanese translation of the article title (accurate, concise)",
  "summary_ja": "3-line summary in Japanese. Line 1: key facts. \
Line 2: industry impact. Line 3: investment implications. \
Separate lines with \\n",
  "impact_level": "one of: low, medium, high, critical — how much this \
news affects the market. low = minimal impact, medium = notable but limited, \
high = significant market implications, critical = major market-moving event",
  "reasoning": "Brief explanation in Japanese of why you assigned \
this impact level"
}}

Rules:
- All Japanese text must be natural, professional Japanese
- impact_level MUST be exactly one of: "low", "medium", "high", "critical"
- If description is empty, analyze based on the title alone
- When full article content is provided, use it for deeper analysis
"""


class GeminiAnalyzer(BaseAnalyzer):
    """Gemini API implementation of BaseAnalyzer."""

    MODEL = "gemini-2.5-flash-lite"
    RPM = 50
    RPD = 1500

    def __init__(
        self,
        *,
        rpm_limiter: RateLimiter | None = None,
        rpd_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(rpm_limiter=rpm_limiter, rpd_limiter=rpd_limiter)
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AnalysisDomainError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def analyze(
        self,
        title: str,
        description: str | None,
        content: str | None = None,
        keywords_by_category: dict[str, list[str]] | None = None,
    ) -> AnalysisData:
        """Build prompt, call API with retry, and parse the response."""
        content_section = ""
        if content:
            truncated = content[: settings.content_max_length]
            content_section = f"\nArticle full text:\n{truncated}\n"

        prompt = ANALYSIS_PROMPT_BASE.format(
            title=title,
            description=description or "(no description available)",
            content_section=content_section,
        )

        if keywords_by_category:
            lines = []
            for cat_slug, kws in keywords_by_category.items():
                kw_list = ", ".join(f'"{kw}"' for kw in kws)
                lines.append(f"- {cat_slug}: [{kw_list}]")
            candidates_block = "\n".join(lines)
            prompt += (
                f"\nAdditionally, select up to 3 keywords from the following "
                f"candidates that best describe this article's topic. Return them "
                f'in a "keywords" field as a JSON array of strings. Only select '
                f"keywords that are clearly related to the article content. "
                f"If none are relevant, return an empty array.\n"
                f"Keyword candidates by category:\n{candidates_block}\n"
            )

        raw_text = await self._call_with_retry(prompt)
        return self._parse_response(raw_text, keywords_by_category)

    async def _call_api(self, prompt: str) -> str:
        """Call Gemini generate_content API."""
        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048,
            ),
        )
        if response.text is None:
            raise AnalysisDomainError("Gemini returned empty response")
        return response.text

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Classify Gemini SDK exceptions into the error hierarchy."""
        if isinstance(exc, ClientError):
            if exc.code == 429:
                return RateLimitError(str(exc))
            if 400 <= exc.code < 500:
                return InvalidInputError(str(exc))
            if exc.code >= 500:
                return TransientError(str(exc))

        # Fallback: check string for common rate limit indicators
        error_str = str(exc).lower()
        if any(
            p in error_str
            for p in ("429", "resource_exhausted", "rate limit", "quota exceeded")
        ):
            return RateLimitError(str(exc))

        # Network / timeout / unknown → transient
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return TransientError(str(exc))

        return AnalysisDomainError(str(exc))

    def _parse_response(
        self, raw_text: str, keywords_by_category: dict[str, list[str]] | None = None
    ) -> AnalysisData:
        """Parse and validate the JSON response from Gemini."""
        text = raw_text.strip()

        # Strip markdown code fences if present
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
            raise AnalysisDomainError(f"Failed to parse Gemini response as JSON: {e}")

        try:
            impact_level = ImpactLevel(data["impact_level"])

            # Parse keywords: filter to valid candidates, max 3
            keywords: list[str] | None = None
            raw_keywords = data.get("keywords")
            if isinstance(raw_keywords, list) and keywords_by_category:
                all_candidates: set[str] = set()
                for kws in keywords_by_category.values():
                    all_candidates.update(kws)
                keywords = [
                    k
                    for k in raw_keywords
                    if isinstance(k, str) and k in all_candidates
                ][:3]
                if not keywords:
                    keywords = None

            return AnalysisData(
                title=str(data["title_ja"]),
                summary=str(data["summary_ja"]),
                impact_level=impact_level,
                reasoning=str(data.get("reasoning", "")),
                keywords=keywords,
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.error(
                "gemini_validation_error",
                data=data,
                error=str(e),
            )
            raise AnalysisDomainError(f"Invalid analysis data from Gemini: {e}")
