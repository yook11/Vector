"""Stage 4 assessor の Gemini response schema。

整合性ドリフト (enum 追加忘れ等) は domain / spec tests で構造的に検出する。
"""

from __future__ import annotations

from typing import Any

from app.analysis.assessment.domain.result import (
    MentionType,
    assessment_category_values,
)

_CATEGORY_VALUES = list(assessment_category_values())
_MENTION_TYPE_VALUES = [m.value for m in MentionType]

ASSESSMENT_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["category", "investor_take", "key_points"],
    "properties": {
        "category": {
            "type": "STRING",
            "enum": _CATEGORY_VALUES,
            "description": (
                "Vector の 12 カテゴリ (先端テック 11 + other) のいずれか、"
                "または out_of_scope"
            ),
        },
        "investor_take": {
            "type": "STRING",
            "description": "日本語の投資家向け論評(短文、空文字不可)",
        },
        "key_points": {
            "type": "ARRAY",
            "description": (
                "記事の重要な情報と登場固有名のペア配列。"
                "重要な情報が無ければ空配列でも可"
            ),
            "items": {
                "type": "OBJECT",
                "required": ["content", "mentions"],
                "properties": {
                    "content": {
                        "type": "STRING",
                        "description": "投資判断に資する重要な情報 (日本語)",
                    },
                    "mentions": {
                        "type": "ARRAY",
                        "description": (
                            "key_point に登場した固有名のみ "
                            "(登場しない固有名は含めない)"
                        ),
                        "items": {
                            "type": "OBJECT",
                            "required": ["surface", "type"],
                            "properties": {
                                "surface": {
                                    "type": "STRING",
                                    "description": (
                                        "固有名の表記 (原文/翻訳どちらでも可)"
                                    ),
                                },
                                "type": {
                                    "type": "STRING",
                                    "enum": _MENTION_TYPE_VALUES,
                                    "description": (
                                        "company / government / academic / "
                                        "product / technology / person"
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}
