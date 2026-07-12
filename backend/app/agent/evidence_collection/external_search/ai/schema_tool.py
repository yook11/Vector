"""DeepSeek strict function-calling schemas for external search adapters."""

from __future__ import annotations

from typing import Any

from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK,
    EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK,
    EXTERNAL_TASK_QUERY_LIMIT,
)

QUERY_GENERATOR_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["queries"],
    "properties": {
        "queries": {
            "type": "array",
            "description": (
                f"1 to {EXTERNAL_TASK_QUERY_LIMIT} short English keyword "
                "queries for external news search."
            ),
            "items": {"type": "string"},
        },
    },
}


EVIDENCE_SELECTOR_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["selections", "missing"],
    "properties": {
        "selections": {
            "type": "array",
            "description": (
                "Useful candidates only, at most "
                f"{EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK}. "
                "Empty if none are useful."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidate_index", "claim", "why_selected"],
                "properties": {
                    "candidate_index": {"type": "integer"},
                    "claim": {"type": "string"},
                    "why_selected": {"type": "string"},
                },
            },
        },
        "missing": {
            "type": "array",
            "description": (
                f"At most {EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK} short "
                "Japanese notes on what could not be confirmed."
            ),
            "items": {"type": "string"},
        },
    },
}
