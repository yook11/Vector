"""Gemini AI analyzer — concrete implementation using Google GenAI SDK."""

import asyncio
import json
import re

import structlog
from google import genai
from google.genai.types import GenerateContentConfig

from app.config import settings
from app.models.analysis import Sentiment
from app.services.ai_analyzer import (
    AnalysisData,
    AnalysisError,
    BaseAnalyzer,
    RateLimitError,
)

logger = structlog.get_logger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds, exponential backoff: 2, 4, 8
MAX_RATE_LIMIT_RETRIES = 1  # independent of normal retry budget
_DAILY_QUOTA_THRESHOLD = 120  # retryDelay above this → daily quota exhausted

VALID_CATEGORIES = {
    "competitive_edge",
    "financial_signal",
    "growth_catalyst",
    "market_disruption",
    "regulatory_shift",
    "risk_mitigation",
}

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
  "sentiment": "one of: positive, negative, neutral",
  "impact_score": <integer 1-10, where 10 = highest market impact>,
  "investment_categories": ["category_slug_1", "category_slug_2"],
  "reasoning": "Brief explanation in Japanese of why you assigned \
this sentiment and impact score"
}}

Rules:
- All Japanese text must be natural, professional Japanese
- sentiment MUST be exactly one of: "positive", "negative", "neutral"
- impact_score MUST be an integer from 1 to 10
- investment_categories: choose 1-3 from EXACTLY this list: \
"growth_catalyst" (new products, market expansion, partnerships), \
"risk_mitigation" (lawsuit wins, regulatory clearance, safety confirmation), \
"competitive_edge" (tech breakthroughs, patents, market share gains), \
"regulatory_shift" (new regulations, policy changes, subsidies, export controls), \
"financial_signal" (earnings, revenue changes, margins, fundraising), \
"market_disruption" (new tech threatening existing markets, industry restructuring). \
Select categories that explain WHY this sentiment/impact was assigned.
- If description is empty, analyze based on the title alone
- When full article content is provided, use it for deeper analysis
"""


def _is_rate_limit_error(exc: Exception) -> bool:
    """Check if the exception is a 429 rate limit error from the GenAI SDK."""
    try:
        from google.genai.errors import ClientError

        return isinstance(exc, ClientError) and exc.code == 429
    except ImportError:
        return False


def _parse_retry_delay(exc: Exception) -> float | None:
    """Extract retryDelay seconds from a Gemini 429 error message.

    Returns the delay in seconds, or None if not found.
    """
    match = re.search(r"retryDelay.*?(\d+)", str(exc))
    return float(match.group(1)) if match else None


class GeminiAnalyzer(BaseAnalyzer):
    """Gemini API implementation of BaseAnalyzer."""

    def __init__(self) -> None:
        api_key = settings.gemini_api_key
        if not api_key:
            raise AnalysisError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return settings.ai_model_name

    async def analyze(
        self,
        title: str,
        description: str | None,
        content: str | None = None,
        keywords_by_category: dict[str, list[str]] | None = None,
    ) -> AnalysisData:
        """Call Gemini API with retry and parse the response."""
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

    async def _call_with_retry(self, prompt: str) -> str:
        """Call Gemini API with two-tier retry strategy.

        - Rate limit (429): parse retryDelay from error. If <= threshold,
          wait and retry without consuming normal retry budget (up to
          MAX_RATE_LIMIT_RETRIES times). Otherwise treat as daily quota
          exhaustion and raise immediately.
        - Other errors: exponential backoff (2, 4, 8 seconds).

        Raises:
            RateLimitError: If rate limit retries exhausted or daily quota hit.
            AnalysisError: After MAX_RETRIES failures for non-429 errors.
        """
        last_error: Exception | None = None
        attempt = 0
        rate_limit_retries = 0

        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                logger.info(
                    "gemini_api_call",
                    attempt=attempt,
                    model=settings.ai_model_name,
                )
                response = await self._client.aio.models.generate_content(
                    model=settings.ai_model_name,
                    contents=prompt,
                    config=GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=2048,
                    ),
                )

                if response.text is None:
                    raise AnalysisError("Gemini returned empty response")

                logger.info("gemini_api_success", attempt=attempt)
                return response.text

            except AnalysisError:
                raise
            except Exception as e:
                last_error = e

                if _is_rate_limit_error(e):
                    retry_delay = _parse_retry_delay(e)

                    # Daily quota: retryDelay too long or missing → stop
                    if retry_delay is None or retry_delay > _DAILY_QUOTA_THRESHOLD:
                        logger.warning(
                            "gemini_daily_quota_exhausted",
                            attempt=attempt,
                            retry_delay=retry_delay,
                            error=str(e),
                        )
                        raise RateLimitError(
                            f"Gemini daily quota exhausted (429): {e}"
                        ) from e

                    # RPM limit: wait retryDelay + margin, then retry
                    rate_limit_retries += 1
                    wait_seconds = retry_delay + 5
                    logger.warning(
                        "gemini_rpm_rate_limited",
                        attempt=attempt,
                        rate_limit_retry=rate_limit_retries,
                        retry_delay=retry_delay,
                        wait_seconds=wait_seconds,
                        error=str(e),
                    )
                    if rate_limit_retries <= MAX_RATE_LIMIT_RETRIES:
                        await asyncio.sleep(wait_seconds)
                        attempt -= 1  # don't consume normal retry budget
                        continue

                    # RPM retries exhausted
                    raise RateLimitError(
                        f"Gemini RPM limit exceeded after "
                        f"{rate_limit_retries} retries: {e}"
                    ) from e

                # Non-rate-limit error: standard exponential backoff
                logger.warning(
                    "gemini_api_error",
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)

        raise AnalysisError(
            f"Gemini API failed after {MAX_RETRIES} attempts: {last_error}"
        )

    def _parse_response(
        self, raw_text: str, keywords_by_category: dict[str, list[str]] | None = None
    ) -> AnalysisData:
        """Parse and validate the JSON response from Gemini.

        Raises:
            AnalysisError: If JSON parsing or validation fails.
        """
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
            raise AnalysisError(f"Failed to parse Gemini response as JSON: {e}")

        # Validate required fields
        try:
            sentiment = Sentiment(data["sentiment"])

            impact_score = int(data["impact_score"])
            if not (1 <= impact_score <= 10):
                raise ValueError(f"impact_score out of range: {impact_score}")

            # Parse investment categories: filter to valid slugs, max 3
            raw_categories = data.get("investment_categories")
            investment_categories: list[str] | None = None
            if isinstance(raw_categories, list):
                investment_categories = [
                    c
                    for c in raw_categories
                    if isinstance(c, str) and c in VALID_CATEGORIES
                ][:3]
                if not investment_categories:
                    investment_categories = None

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
                sentiment=sentiment,
                impact_score=impact_score,
                reasoning=data.get("reasoning"),
                investment_categories=investment_categories,
                keywords=keywords,
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.error(
                "gemini_validation_error",
                data=data,
                error=str(e),
            )
            raise AnalysisError(f"Invalid analysis data from Gemini: {e}")
