"""Question planner Gemini response schema."""

from __future__ import annotations

from typing import Any, get_args

from app.agent.contract import PlanType
from app.agent.planning.contract import (
    MAX_ARTICLE_SEARCH_QUERIES,
    RESEARCH_TASK_LIMIT,
    TargetTimeWindowKind,
)

_TARGET_TIME_WINDOW_KIND_VALUES = list(get_args(TargetTimeWindowKind))

QUESTION_PLANNER_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": [
        "plan_type",
        "research_tasks",
    ],
    "properties": {
        "plan_type": {
            "type": "STRING",
            "enum": list(get_args(PlanType)),
            "description": "Answer plan: direct_answer or search.",
        },
        "research_tasks": {
            "type": "ARRAY",
            "maxItems": RESEARCH_TASK_LIMIT,
            "description": (
                "Research tasks, each pairing one research goal with the "
                "internal search queries scoped to it."
            ),
            "items": {
                "type": "OBJECT",
                "required": [
                    "research_goal",
                    "article_search_queries",
                ],
                "properties": {
                    "research_goal": {
                        "type": "STRING",
                        "description": "One research goal for external news search.",
                    },
                    "article_search_queries": {
                        "type": "ARRAY",
                        "maxItems": MAX_ARTICLE_SEARCH_QUERIES,
                        "description": (
                            "Queries for Vector analyzed article retrieval, "
                            "scoped to this research task."
                        ),
                        "items": {
                            "type": "STRING",
                            "description": (
                                "One analyzed-article semantic search query."
                            ),
                        },
                    },
                },
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
