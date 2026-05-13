"""Stage 4 assessor 用 AI 境界 schema 定数 (provider 別)。

provider ごとに SDK の schema 受理形式が違うため、別 SSoT として並存させる:

- ``ASSESSMENT_TOOL_SCHEMA`` (DeepSeek): Function Calling + ``strict: true``
  (beta endpoint) 用。lowercase 標準 JSON Schema 形式で ``additionalProperties:
  false`` + ``pattern`` を入れる。``$ref``/``$defs`` は AI が enforce しないので
  inline flat (specs/stage2-deepseek-migration.md PoC 参照)。
- ``ASSESSMENT_GEMINI_SCHEMA`` (Gemini): ``response_schema`` 引数用。
  OpenAPI 3.0 subset (``type: "OBJECT"`` / ``"STRING"`` の uppercase) で SDK
  Schema 形式に寄せる。``additionalProperties`` は Gemini SDK Schema 形式で
  未サポート、``pattern`` は enforce 弱いため省略する。受信後の制約検証は
  ``parse_assessment`` 内 ``TopicName`` VO で完全実施するため AI 境界での
  pattern / additionalProperties 強制は冗長。

整合性ドリフト (enum 追加忘れ等) は ``tests/analysis/assessment/domain/test_result.py``
で構造的に検出する。
"""

from __future__ import annotations

from typing import Any

from app.analysis.assessment.domain.result import MentionType, ValidCategory

_MENTION_TYPE_VALUES = [m.value for m in MentionType]

ASSESSMENT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "topic", "investor_take", "events"],
    "properties": {
        "category": {
            "type": "string",
            "enum": [c.value for c in ValidCategory],
            "description": (
                "Vector の 12 カテゴリ (先端テック 11 + other) のいずれか、"
                "または out_of_scope"
            ),
        },
        "topic": {
            "type": "string",
            "pattern": r"^[a-z0-9]+( [a-z0-9]+)*$",
            "description": (
                "正規化済み英語小文字 1-3 語のラベル。例: 'ai agents'、"
                "'quantum computing'、'6g'。日本語不可、大文字不可、"
                "ハイフン/アンダースコア不可、冠詞 (a/an/the/in/of) 不可。"
                "本フィールドは event-extraction 移行期 (PR1-4) の並列出力で、"
                "events への完全移行後に削除予定"
            ),
        },
        "investor_take": {
            "type": "string",
            "description": "日本語の投資家向け論評（短文、空文字不可）",
        },
        "events": {
            "type": "array",
            "description": (
                "記事内で起きた event と登場固有名のペア配列。"
                "重要な event が無ければ空配列でも可"
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "mentions"],
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "何が起きたかを表す短文 (日本語)",
                    },
                    "mentions": {
                        "type": "array",
                        "description": (
                            "event に登場した固有名のみ (登場しない固有名は含めない)"
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


ASSESSMENT_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["category", "topic", "investor_take", "events"],
    "properties": {
        "category": {
            "type": "STRING",
            "enum": [c.value for c in ValidCategory],
            "description": (
                "Vector の 12 カテゴリ (先端テック 11 + other) のいずれか、"
                "または out_of_scope"
            ),
        },
        "topic": {
            "type": "STRING",
            "description": (
                "正規化済み英語小文字 1-3 語のラベル。例: 'ai agents'、"
                "'quantum computing'、'6g'。日本語不可、大文字不可、"
                "ハイフン/アンダースコア不可、冠詞 (a/an/the/in/of) 不可。"
                "本フィールドは event-extraction 移行期 (PR1-4) の並列出力で、"
                "events への完全移行後に削除予定"
            ),
            # NOTE: ``pattern`` は Gemini SDK Schema 形式で enforce が弱いため
            # 付けない。受信後 TopicName VO の正規化制約で reject
            # (parse_assessment 経由)。
        },
        "investor_take": {
            "type": "STRING",
            "description": "日本語の投資家向け論評（短文、空文字不可）",
        },
        "events": {
            "type": "ARRAY",
            "description": (
                "記事内で起きた event と登場固有名のペア配列。"
                "重要な event が無ければ空配列でも可"
            ),
            "items": {
                "type": "OBJECT",
                "required": ["description", "mentions"],
                "properties": {
                    "description": {
                        "type": "STRING",
                        "description": "何が起きたかを表す短文 (日本語)",
                    },
                    "mentions": {
                        "type": "ARRAY",
                        "description": (
                            "event に登場した固有名のみ (登場しない固有名は含めない)"
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
    # NOTE: ``additionalProperties`` は Gemini SDK Schema 形式 (OpenAPI 3.0 subset)
    # で未サポート。AI が余分 key を返しても parse_assessment は 4 key のみ
    # 取り出すため、AI 境界での strict enforcement は冗長。
}
