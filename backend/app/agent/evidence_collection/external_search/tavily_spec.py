"""Tavily news search をどう呼ぶかの宣言。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchDateFilter,
)

__all__ = [
    "TAVILY_NEWS_SEARCH_SPEC",
    "TavilySearchCallSpec",
    "build_search_body",
]


@dataclass(frozen=True, slots=True)
class TavilySearchCallSpec:
    """Tavily search API の呼び出し設定。"""

    search_url: str
    request_timeout_seconds: int
    max_results_limit: int
    topic: str
    search_depth: str
    include_answer: bool
    include_raw_content: bool
    start_date_backoff_days: int


TAVILY_NEWS_SEARCH_SPEC: Final[TavilySearchCallSpec] = TavilySearchCallSpec(
    search_url="https://api.tavily.com/search",
    request_timeout_seconds=10,
    max_results_limit=20,
    topic="news",
    search_depth="basic",
    include_answer=False,
    include_raw_content=False,
    # 半開のstart_dateをTavilyの包含境界へ保守的に寄せる。
    start_date_backoff_days=1,
)


def build_search_body(
    spec: TavilySearchCallSpec,
    *,
    query: str,
    limit: int,
    date_filter: ExternalSearchDateFilter | None,
) -> dict[str, object]:
    """specの固定値と1回分の条件からrequest bodyを組み立てる。"""
    body: dict[str, object] = {
        "query": query,
        "topic": spec.topic,
        "search_depth": spec.search_depth,
        "max_results": min(limit, spec.max_results_limit),
        "include_answer": spec.include_answer,
        "include_raw_content": spec.include_raw_content,
    }
    if date_filter is not None:
        start_date = date_filter.start_date - timedelta(
            days=spec.start_date_backoff_days
        )
        body["start_date"] = start_date.isoformat()
        body["end_date"] = date_filter.end_date.isoformat()
    return body
