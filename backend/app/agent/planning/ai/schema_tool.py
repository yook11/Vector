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

_TARGET_TIME_WINDOW_KIND_VALUES = [
    "today",
    "yesterday",
    "last_n_days",
    "this_week",
    "last_week",
    "this_month",
    "calendar_month",
    "date_range",
    "unsupported_explicit_window",
]

QUESTION_PLANNER_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": [
        "retrieval_mode",
        "internal_queries",
        "external_collection_goals",
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
        "external_collection_goals": {
            "type": "ARRAY",
            "description": (
                "External research goals describing what evidence to collect. "
                "Short Japanese sentences. Return at most 3 items."
            ),
            "items": {
                "type": "STRING",
                "description": "One research goal for external news search.",
            },
        },
        "target_time_window": {
            "type": "OBJECT",
            "nullable": True,
            "required": ["kind"],
            "description": (
                "Optional publication window for external evidence. Null means "
                "publication date is intentionally unrestricted."
            ),
            "properties": {
                "kind": {
                    "type": "STRING",
                    "enum": _TARGET_TIME_WINDOW_KIND_VALUES,
                },
                "year": {
                    "type": "INTEGER",
                    "minimum": 1,
                    "maximum": 9999,
                    "nullable": True,
                },
                "month": {
                    "type": "INTEGER",
                    "minimum": 1,
                    "maximum": 12,
                    "nullable": True,
                },
                "days": {
                    "type": "INTEGER",
                    "minimum": 1,
                    "maximum": 60,
                    "nullable": True,
                },
                "start_date": {
                    "type": "STRING",
                    "format": "date",
                    "nullable": True,
                },
                "end_date_inclusive": {
                    "type": "STRING",
                    "format": "date",
                    "nullable": True,
                },
            },
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
