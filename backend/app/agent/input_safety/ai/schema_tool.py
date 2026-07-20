"""Gemini response schema for input safety checks."""

from __future__ import annotations

from typing import Any

from app.agent.input_safety.contract import InputSafetyAgentBlockReason

INPUT_SAFETY_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["input_safety_result", "block_reason"],
    "properties": {
        "input_safety_result": {
            "type": "STRING",
            "enum": ["allow", "block"],
        },
        "block_reason": {
            "type": "STRING",
            "enum": [reason.value for reason in InputSafetyAgentBlockReason],
            "nullable": True,
        },
    },
}
