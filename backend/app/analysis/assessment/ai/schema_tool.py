"""Stage 4 assessor 用 AI 境界 schema 定数。

``ASSESSMENT_TOOL_SCHEMA`` (DeepSeek): Function Calling + ``strict: true``
(beta endpoint) 用。lowercase 標準 JSON Schema 形式で ``additionalProperties:
false`` + ``pattern`` を入れる。``$ref``/``$defs`` は AI が enforce しないので
inline flat (specs/stage2-deepseek-migration.md PoC 参照)。

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

ASSESSMENT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "investor_take", "key_points"],
    "properties": {
        "category": {
            "type": "string",
            "enum": _CATEGORY_VALUES,
            "description": (
                "Vector の 12 カテゴリ (先端テック 11 + other) のいずれか、"
                "または out_of_scope"
            ),
        },
        "investor_take": {
            "type": "string",
            "description": "日本語の投資家向け論評(短文、空文字不可)",
        },
        "key_points": {
            "type": "array",
            "description": (
                "記事の重要な情報と登場固有名のペア配列。"
                "重要な情報が無ければ空配列でも可"
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["content", "mentions"],
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "投資判断に資する重要な情報 (日本語)",
                    },
                    "mentions": {
                        "type": "array",
                        "description": (
                            "key_point に登場した固有名のみ "
                            "(登場しない固有名は含めない)"
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["surface", "type"],
                            "properties": {
                                "surface": {
                                    "type": "string",
                                    "description": (
                                        "固有名の表記 (原文/翻訳どちらでも可)"
                                    ),
                                },
                                "type": {
                                    "type": "string",
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
