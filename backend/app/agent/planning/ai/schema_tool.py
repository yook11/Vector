"""Question planner Gemini response schema."""

from __future__ import annotations

from typing import Any, get_args

from app.agent.contract import PlanType

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
        "plan_type",
        "article_search_queries",
        "research_goals",
    ],
    "properties": {
        "plan_type": {
            "type": "STRING",
            "enum": list(get_args(PlanType)),
            "description": "Answer plan: direct_answer or search.",
        },
        "article_search_queries": {
            "type": "ARRAY",
            "description": (
                "Queries to embed for Vector analyzed article retrieval. "
                "Do not simply copy the raw user question. "
                "Return at most 3 items."
            ),
            "items": {
                "type": "STRING",
                "description": "One analyzed-article semantic search query.",
            },
        },
        "research_goals": {
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
    },
}


def plan_type_values() -> list[PlanType]:
    """Return values to keep tests close to the schema SSoT."""

    return list(get_args(PlanType))
