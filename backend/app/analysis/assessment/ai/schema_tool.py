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

整合性ドリフト (enum 追加忘れ等) は ``test_schema.py`` で構造的に検出する。
"""

from __future__ import annotations

from typing import Any

from app.analysis.assessment.ai.schema import ValidCategory

ASSESSMENT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "topic", "investor_take"],
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
                "ハイフン/アンダースコア不可、冠詞 (a/an/the/in/of) 不可"
            ),
        },
        "investor_take": {
            "type": "string",
            "description": "日本語の投資家向け論評（短文、空文字不可）",
        },
    },
}


ASSESSMENT_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["category", "topic", "investor_take"],
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
                "ハイフン/アンダースコア不可、冠詞 (a/an/the/in/of) 不可"
            ),
            # NOTE: ``pattern`` は Gemini SDK Schema 形式で enforce が弱いため
            # 付けない。受信後 TopicName VO の正規化制約で reject
            # (parse_assessment 経由)。
        },
        "investor_take": {
            "type": "STRING",
            "description": "日本語の投資家向け論評（短文、空文字不可）",
        },
    },
    # NOTE: ``additionalProperties`` は Gemini SDK Schema 形式 (OpenAPI 3.0 subset)
    # で未サポート。AI が余分 key を返しても parse_assessment は 3 key のみ
    # 取り出すため、AI 境界での strict enforcement は冗長。
}
