"""Gemini AI analyzer — concrete implementation using Google GenAI SDK."""

import asyncio
import json

import structlog
from google import genai
from google.genai.types import GenerateContentConfig

from app.config import settings
from app.services.ai_analyzer import (
    AnalysisData,
    AnalysisError,
    BaseAnalyzer,
    RateLimitError,
)

logger = structlog.get_logger(__name__)

GEMINI_MODEL = "gemini-2.5-flash-lite"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds, exponential backoff: 2, 4, 8

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
  "key_topics": ["topic1_ja", "topic2_ja", "topic3_ja"],
  "reasoning": "Brief explanation in Japanese of why you assigned \
this sentiment and impact score"
}}

Rules:
- All Japanese text must be natural, professional Japanese
- sentiment MUST be exactly one of: "positive", "negative", "neutral"
- impact_score MUST be an integer from 1 to 10
- key_topics should contain 2-5 Japanese topic keywords
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
        return GEMINI_MODEL

    async def analyze(
        self,
        title: str,
        description: str | None,
        content: str | None = None,
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
        raw_text = await self._call_with_retry(prompt)
        return self._parse_response(raw_text)

    async def _call_with_retry(self, prompt: str) -> str:
        """Call Gemini API with exponential backoff retry.

        Raises:
            AnalysisError: After MAX_RETRIES failures.
        """
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    "gemini_api_call",
                    attempt=attempt,
                    model=GEMINI_MODEL,
                )
                response = await self._client.aio.models.generate_content(
                    model=GEMINI_MODEL,
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
                # 429: SDK already retried 4 times (5 total attempts).
                # Quota is exhausted — propagate immediately, no app-level retry.
                if _is_rate_limit_error(e):
                    logger.warning(
                        "gemini_rate_limit_exhausted",
                        attempt=attempt,
                        error=str(e),
                    )
                    raise RateLimitError(
                        f"Gemini API rate limit exceeded (429): {e}"
                    ) from e

                last_error = e
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

    def _parse_response(self, raw_text: str) -> AnalysisData:
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
            sentiment = data["sentiment"]
            if sentiment not in ("positive", "negative", "neutral"):
                raise ValueError(f"Invalid sentiment: {sentiment}")

            impact_score = int(data["impact_score"])
            if not (1 <= impact_score <= 10):
                raise ValueError(f"impact_score out of range: {impact_score}")

            key_topics = data.get("key_topics")
            if key_topics is not None and not isinstance(key_topics, list):
                key_topics = None

            return AnalysisData(
                title_ja=str(data["title_ja"]),
                summary_ja=str(data["summary_ja"]),
                sentiment=sentiment,
                impact_score=impact_score,
                key_topics=key_topics,
                reasoning=data.get("reasoning"),
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.error(
                "gemini_validation_error",
                data=data,
                error=str(e),
            )
            raise AnalysisError(f"Invalid analysis data from Gemini: {e}")
