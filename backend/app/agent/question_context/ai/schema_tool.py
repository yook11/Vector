"""Gemini response schema for question context generation."""

from __future__ import annotations

from typing import Any

QUESTION_CONTEXT_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": [
        "standalone_question",
        "content_requirements",
        "response_requirements",
        "relevant_prior_coverage",
        "active_goal",
        "explicit_feedback_detected",
    ],
    "properties": {
        "standalone_question": {
            "type": "STRING",
            "description": "A self-contained Japanese question for retrieval.",
        },
        "content_requirements": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "What the answer must cover, or an empty array.",
        },
        "response_requirements": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "How the answer should be delivered, or an empty array.",
        },
        "relevant_prior_coverage": {
            "type": "STRING",
            "description": (
                "Already covered content to avoid repeating, or an empty string."
            ),
        },
        "active_goal": {
            "type": "STRING",
            "description": "The evidenced research flow, or an empty string.",
        },
        "explicit_feedback_detected": {
            "type": "BOOLEAN",
            "description": (
                "Whether the current question explicitly flags prior failure."
            ),
        },
    },
}
