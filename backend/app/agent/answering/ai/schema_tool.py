"""Evidence answer Gemini response schema."""

from __future__ import annotations

from typing import Any

_SUFFICIENCY_VALUES = ["answered", "insufficient"]

EVIDENCE_ANSWER_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": [
        "sufficiency",
        "answer",
        "cited_refs",
        "missing_aspects",
    ],
    "properties": {
        "sufficiency": {
            "type": "STRING",
            "enum": _SUFFICIENCY_VALUES,
            "description": (
                "Whether the evidence is sufficient for a sourced answer. "
                "Use insufficient when citable evidence is missing or partial."
            ),
        },
        "answer": {
            "type": "STRING",
            "description": (
                "Japanese answer shown to the user. It must be non-empty. "
                "When evidence is missing, clearly say that no citable evidence "
                "was found before giving any cautious reference answer."
            ),
        },
        "cited_refs": {
            "type": "ARRAY",
            "description": (
                "source_ref values cited by the answer. Use only refs present in "
                "the evidence block. Use an empty list when there is no citable "
                "evidence."
            ),
            "items": {
                "type": "STRING",
                "description": "One source_ref from the evidence block.",
            },
        },
        "missing_aspects": {
            "type": "ARRAY",
            "description": (
                "Japanese descriptions of missing evidence or uncertainty. "
                "Required to be non-empty when sufficiency is insufficient."
            ),
            "items": {
                "type": "STRING",
                "description": "One missing evidence aspect.",
            },
        },
    },
}
