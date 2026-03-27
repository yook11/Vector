"""Compare gemini-2.5-flash vs gemini-2.5-flash-lite analysis quality.

Usage:
    docker compose exec backend python -m app.scripts.compare_models --count 5
"""

import argparse
import asyncio
import json
import sys

from google import genai
from google.genai.types import GenerateContentConfig
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.db import engine
from app.models.analysis import AnalysisResult
from app.models.news import NewsArticle
from app.services.ai_analyzer import AnalysisData, AnalysisError
from app.services.gemini_analyzer import (
    ANALYSIS_PROMPT_BASE,
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    _is_rate_limit_error,
)

FLASH_LITE_MODEL = "gemini-2.5-flash-lite"
REQUEST_INTERVAL = 4.0  # seconds between API calls (flash-lite: 15 RPM)


async def call_flash_lite(client: genai.Client, prompt: str) -> str:
    """Call flash-lite API with retry logic."""
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await client.aio.models.generate_content(
                model=FLASH_LITE_MODEL,
                contents=prompt,
                config=GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=2048,
                ),
            )
            if response.text is None:
                raise AnalysisError("Flash-lite returned empty response")
            return response.text
        except AnalysisError:
            raise
        except Exception as e:
            if _is_rate_limit_error(e):
                raise AnalysisError(f"Rate limit exceeded: {e}") from e
            last_error = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

    raise AnalysisError(
        f"Flash-lite API failed after {MAX_RETRIES} attempts: {last_error}"
    )


def parse_response(raw_text: str) -> AnalysisData:
    """Parse JSON response, stripping markdown fences if present."""
    text = raw_text.strip()

    # Strip markdown code fences (flash-lite may wrap JSON in ```json ... ```)
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    data = json.loads(text)

    sentiment = data["sentiment"]
    if sentiment not in ("positive", "negative", "neutral"):
        raise ValueError(f"Invalid sentiment: {sentiment}")

    impact_score = int(data["impact_score"])
    if not (1 <= impact_score <= 10):
        raise ValueError(f"impact_score out of range: {impact_score}")

    return AnalysisData(
        title=str(data["title_ja"]),
        summary=str(data["summary_ja"]),
        sentiment=sentiment,
        impact_score=impact_score,
        reasoning=data.get("reasoning"),
    )


def print_comparison(
    idx: int,
    total: int,
    article: NewsArticle,
    flash_result: AnalysisResult,
    lite_result: AnalysisData | None,
    error: str | None,
) -> dict:
    """Print side-by-side comparison and return stats."""
    title_display = article.title_original[:60]
    print(f"\n{'=' * 70}")
    print(f"  Article {idx}/{total}: {title_display}")
    print(f"{'=' * 70}")

    if error:
        print(f"  [ERROR] flash-lite failed: {error}")
        return {"parse_ok": False, "sentiment_match": False, "score_diff": None}

    assert lite_result is not None

    # Get flash title/summary from translations
    flash_title = ""
    flash_summary = ""
    for t in flash_result.translations:
        if t.locale == "ja":
            flash_title = t.title
            flash_summary = t.summary
            break

    # title
    print("\n  [title]")
    print(f"    flash:      {flash_title}")
    print(f"    flash-lite: {lite_result.title}")

    # summary
    print("\n  [summary]")
    print("    flash:")
    for line in flash_summary.split("\n"):
        print(f"      {line}")
    print("    flash-lite:")
    for line in lite_result.summary.split("\n"):
        print(f"      {line}")

    # sentiment
    s_match = flash_result.sentiment == lite_result.sentiment
    match_label = "MATCH" if s_match else "MISMATCH"
    print(
        f"\n  [sentiment]  flash: {flash_result.sentiment}"
        f" | flash-lite: {lite_result.sentiment}  {match_label}"
    )

    # impact_score
    diff = lite_result.impact_score - flash_result.impact_score
    sign = "+" if diff > 0 else ""
    print(
        f"  [impact_score]  flash: {flash_result.impact_score}"
        f" | flash-lite: {lite_result.impact_score}  (diff: {sign}{diff})"
    )

    return {
        "parse_ok": True,
        "sentiment_match": s_match,
        "score_diff": abs(diff),
    }


async def main(count: int) -> None:
    api_key = settings.gemini_api_key
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not configured", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    # Fetch analyzed articles from DB
    async with SQLModelAsyncSession(engine) as session:
        stmt = (
            select(NewsArticle)
            .where(
                NewsArticle.id.in_(  # type: ignore[union-attr]
                    select(AnalysisResult.news_article_id)
                )
            )
            .options(
                selectinload(NewsArticle.analyses).selectinload(  # type: ignore[arg-type]
                    AnalysisResult.translations
                )
            )
            .order_by(NewsArticle.fetched_at.desc())  # type: ignore[union-attr]
            .limit(count)
        )
        result = await session.execute(stmt)
        articles: list[NewsArticle] = list(result.scalars().all())

    if not articles:
        print("No analyzed articles found in DB.")
        sys.exit(0)

    print(f"Comparing {len(articles)} articles: flash vs flash-lite")
    print(f"Model: {FLASH_LITE_MODEL}")
    print(f"Request interval: {REQUEST_INTERVAL}s")

    stats: list[dict] = []

    for i, article in enumerate(articles):
        if i > 0:
            await asyncio.sleep(REQUEST_INTERVAL)

        # Build prompt (same as GeminiAnalyzer.analyze)
        content_section = ""
        if article.original_content:
            truncated = article.original_content[: settings.content_max_length]
            content_section = f"\nArticle full text:\n{truncated}\n"

        prompt = ANALYSIS_PROMPT_BASE.format(
            title=article.title_original,
            description=article.description_original or "(no description available)",
            content_section=content_section,
        )

        lite_result: AnalysisData | None = None
        error: str | None = None

        try:
            raw_text = await call_flash_lite(client, prompt)
            lite_result = parse_response(raw_text)
        except Exception as e:
            error = str(e)

        stat = print_comparison(
            idx=i + 1,
            total=len(articles),
            article=article,
            flash_result=article.analyses[0] if article.analyses else None,
            lite_result=lite_result,
            error=error,
        )
        stats.append(stat)

    # Summary
    total = len(stats)
    parse_ok = sum(1 for s in stats if s["parse_ok"])
    sentiment_matches = sum(1 for s in stats if s["sentiment_match"])
    score_diffs = [s["score_diff"] for s in stats if s["score_diff"] is not None]
    avg_diff = sum(score_diffs) / len(score_diffs) if score_diffs else 0

    print(f"\n{'=' * 70}")
    print("  Summary")
    print(f"{'=' * 70}")
    print(f"  Total articles:        {total}")
    print(f"  JSON parse success:    {parse_ok}/{total}")
    if total > 0:
        print(
            f"  Sentiment match:       {sentiment_matches}/{total}"
            f" ({sentiment_matches / total * 100:.0f}%)"
        )
    print(f"  Impact score avg diff: {avg_diff:.1f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Compare gemini-2.5-flash vs flash-lite analysis quality"
    )
    parser.add_argument(
        "--count", type=int, default=5, help="Number of articles to compare"
    )
    args = parser.parse_args()
    asyncio.run(main(args.count))
