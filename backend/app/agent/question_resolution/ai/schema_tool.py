"""Gemini response schema for question resolution."""

from __future__ import annotations

from typing import Any

QUESTION_RESOLUTION_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": [
        "standalone_question",
        "user_intent",
        "prior_coverage",
        "user_activity_context",
    ],
    "properties": {
        "standalone_question": {
            "type": "STRING",
            "description": "A self-contained Japanese question for retrieval.",
        },
        "user_intent": {
            "type": "STRING",
            "description": "How the user wants this response, or an empty string.",
        },
        "prior_coverage": {
            "type": "STRING",
            "description": (
                "Already covered content to avoid repeating, or an empty string."
            ),
        },
        "user_activity_context": {
            "type": "STRING",
            "description": "The evidenced research flow, or an empty string.",
        },
    },
}
