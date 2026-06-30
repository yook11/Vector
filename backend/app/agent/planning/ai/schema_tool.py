"""Question planner Gemini response schema."""

from __future__ import annotations

from typing import Any

from app.agent.contract import RetrievalMode

_RETRIEVAL_MODE_VALUES = [
    "none",
    "internal",
    "external",
    "internal_and_external",
]

QUESTION_PLANNER_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": [
        "retrieval_mode",
        "internal_queries",
        "external_queries",
        "reason",
    ],
    "properties": {
        "retrieval_mode": {
            "type": "STRING",
            "enum": _RETRIEVAL_MODE_VALUES,
            "description": (
                "Needed retrieval: none, internal, external, or internal_and_external."
            ),
        },
        "internal_queries": {
            "type": "ARRAY",
            "description": (
                "Queries to embed for Vector internal article retrieval. "
                "Do not simply copy the raw user question. "
                "Return at most 3 items."
            ),
            "items": {
                "type": "STRING",
                "description": "One internal vector-search query.",
            },
        },
        "external_queries": {
            "type": "ARRAY",
            "description": "Keyword queries for external news search.",
            "items": {
                "type": "STRING",
                "description": "One external news-search keyword query.",
            },
        },
        "target_time_window": {
            "type": "STRING",
            "nullable": True,
            "description": (
                "Optional time window extracted from the question, such as "
                "today, last 24 hours, this week, or a concrete month."
            ),
        },
        "reason": {
            "type": "STRING",
            "description": "Short Japanese routing reason, not shown to users.",
        },
    },
}


def retrieval_mode_values() -> list[RetrievalMode]:
    """Return values to keep tests close to the schema SSoT."""

    return [
        "none",
        "internal",
        "external",
        "internal_and_external",
    ]
