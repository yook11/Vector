"""Probe Tavily news search response shape without storing raw responses."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.evidence_collection.external_search import (
    TAVILY_MAX_RESULTS_LIMIT,
    TAVILY_REQUEST_TIMEOUT_SECONDS,
    TAVILY_SEARCH_URL,
)
from app.config import settings
from app.shared.security.safe_http import make_safe_async_client


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe Tavily topic=news published_date response shape."
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="NVIDIA Blackwell latest news",
    )
    parser.add_argument("--max-results", type=int, default=5)
    return parser


async def _probe(query: str, max_results: int) -> None:
    api_key = settings.tavily_api_key.get_secret_value()
    if not api_key:
        raise SystemExit("TAVILY_API_KEY is not configured")

    body = {
        "query": query,
        "topic": "news",
        "search_depth": "basic",
        "max_results": min(max_results, TAVILY_MAX_RESULTS_LIMIT),
        "include_answer": False,
        "include_raw_content": False,
    }
    async with make_safe_async_client() as client:
        response = await client.post(
            TAVILY_SEARCH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=TAVILY_REQUEST_TIMEOUT_SECONDS,
        )

    if not 200 <= response.status_code < 300:
        raise SystemExit(f"Tavily request failed with status {response.status_code}")

    data = response.json()
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise SystemExit("Tavily response did not contain a results list")

    values = [
        result.get("published_date") for result in results if isinstance(result, dict)
    ]
    present = [value for value in values if isinstance(value, str) and value.strip()]
    type_counts = Counter(type(value).__name__ for value in values)
    samples = _unique_samples(present)

    print(f"result_count={len(results)}")
    print(f"published_date_present={len(present)}")
    print(f"published_date_missing={len(values) - len(present)}")
    print(f"published_date_types={dict(type_counts)}")
    print(f"published_date_samples={samples}")


def _unique_samples(values: Sequence[str], *, limit: int = 5) -> list[str]:
    samples: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        samples.append(value)
        seen.add(value)
        if len(samples) >= limit:
            break
    return samples


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    asyncio.run(_probe(args.query, args.max_results))


if __name__ == "__main__":
    main()
