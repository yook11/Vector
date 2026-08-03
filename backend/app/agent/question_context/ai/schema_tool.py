"""Gemini response schema for question context generation."""

from __future__ import annotations

from typing import Any

QUESTION_CONTEXT_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": [
        "standalone_question",
        "answer_requirements",
        "active_goal",
        "relevant_prior_coverage",
    ],
    "properties": {
        "standalone_question": {
            "type": "STRING",
            "description": (
                "履歴を知らなくても意味が通る形にした現在の質問。"
                "自己完結していればほぼそのまま返す。代名詞・省略は履歴に根拠がある"
                "対象だけを補う。"
            ),
        },
        "answer_requirements": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": (
                "回答が満たすべき条件。ユーザーが明示・含意した要求だけを分解する。"
                "調査観点や比較軸を発明しない。"
            ),
        },
        "active_goal": {
            "type": "STRING",
            "description": (
                "履歴または現在の質問に明確な根拠がある、スレッド全体の作業・調査の"
                "目的。無ければ空文字。"
            ),
        },
        "relevant_prior_coverage": {
            "type": "STRING",
            "description": "今回の質問に関係する既回答の簡潔な要約。無ければ空文字。",
        },
    },
}
